"""Page commit, idempotent comments and continuation share the task transaction."""

from dataclasses import asdict
from uuid import UUID, uuid4

from sqlalchemy import inspect, insert, select

from app.crawlers.product_list import product_id_from_url
from app.crawlers.product_reviews import REVIEW_SOURCE_VERSION
from app.models import CrawlTask, Product, ProductReview, ResourceType, Shop, TaskType
from app.queue.retry_policy import Failure, TaskFailure
from app.services.seed_importer import fingerprint
from app.services.snapshot_service import utc_window
from app.services.task_runtime import database_now


def reviews_available(session):
    return inspect(session.connection()).has_table(ProductReview.__tablename__)


def ensure_review_task(session, settings, product_id, *, max_pages=None, initial_only=False):
    if initial_only and TaskType.PRODUCT_REVIEWS not in settings.enabled_task_types:
        return None, False
    max_pages = settings.review_max_pages if max_pages is None else max_pages
    if type(max_pages) is not int or not 1 <= max_pages <= 500:
        raise ValueError("评论页数必须为1至500的整数。")
    product = session.scalar(select(Product).where(Product.id == product_id,
        Product.platform == settings.platform, Product.region == settings.region).with_for_update())
    if product is None:
        raise ValueError("product_not_found")
    if initial_only:
        if product.last_success_at is None or not product.review_count:
            return None, False
        # The product lock serializes this check with manual collection and
        # other detail workers. Do not reopen an exhausted initial collection
        # on each later detail observation; explicit retries remain available.
        previous = session.scalar(select(CrawlTask).where(
            CrawlTask.platform == product.platform, CrawlTask.region == product.region,
            CrawlTask.task_type == TaskType.PRODUCT_REVIEWS,
            CrawlTask.resource_type == ResourceType.PRODUCT, CrawlTask.resource_id == str(product.id),
        ).order_by(CrawlTask.id.desc()).limit(1).with_for_update())
        if previous is not None:
            return previous, False
    shop = session.get(Shop, product.shop_id)
    if (shop is None or shop.platform != product.platform or shop.region != product.region
            or product_id_from_url(product.product_url) != product.platform_product_id):
        raise ValueError("invalid_review_product")
    values = _review_task_values(product, shop, database_now(session), max_pages)
    previous = session.scalar(select(CrawlTask).where(
        CrawlTask.idempotency_key == values["idempotency_key"]).with_for_update())
    if previous:
        return previous, False
    task = CrawlTask(**values)
    session.add(task)
    session.flush()
    return task, True


def _review_task_values(product, shop, now, max_pages):
    window = utc_window(now, 3600)
    key = fingerprint({"type": "product_reviews", "product": product.id, "window": window.isoformat(),
                       "source_version": REVIEW_SOURCE_VERSION, "max_pages": max_pages})
    task_id = str(uuid4())
    return dict(task_id=task_id, platform=product.platform, region=product.region,
        task_type=TaskType.PRODUCT_REVIEWS, resource_type=ResourceType.PRODUCT, resource_id=str(product.id),
        idempotency_key=key, priority=3, scheduled_for=window, available_at=now,
        payload={"product_id": str(product.id), "platform_product_id": product.platform_product_id,
                 "platform_shop_id": shop.platform_shop_id, "product_url": product.product_url,
                 "root_task_id": task_id, "page_number": 1, "page_size": 5, "max_pages": max_pages,
                 "source_version": REVIEW_SOURCE_VERSION, "seen_page_hashes": []})


def ensure_initial_review_task(session, settings, product_id):
    """Collect the public document once; API pagination remains an explicit job.

    Anonymous pagination is currently unavailable. A one-page budget keeps the
    real public comments and preserves `complete=False` when the source has more.
    """
    return ensure_review_task(session, settings, product_id, max_pages=1, initial_only=True)


def enqueue_initial_review_tasks(session, settings, product_ids):
    """Create a bounded batch with shared lookups and one bulk INSERT.

    Lock products in ID order, then use a current read for existing work. This
    serializes with manual requests and detail workers even under REPEATABLE READ.
    """
    if TaskType.PRODUCT_REVIEWS not in settings.enabled_task_types or not product_ids:
        return 0
    if len(product_ids) > 100 or any(type(value) is not int or value <= 0 for value in product_ids):
        raise ValueError("invalid_review_batch")
    ids = list(dict.fromkeys(product_ids))
    products = {row.id: row for row in session.scalars(select(Product).where(
        Product.id.in_(ids), Product.platform == settings.platform, Product.region == settings.region,
    ).order_by(Product.id).with_for_update().execution_options(populate_existing=True))}
    if len(products) != len(ids):
        raise ValueError("product_not_found")
    existing = set(session.scalars(select(CrawlTask.resource_id).where(
        CrawlTask.platform == settings.platform, CrawlTask.region == settings.region,
        CrawlTask.task_type == TaskType.PRODUCT_REVIEWS, CrawlTask.resource_type == ResourceType.PRODUCT,
        CrawlTask.resource_id.in_([str(value) for value in ids]),
    ).with_for_update()))
    shops = {row.id: row for row in session.scalars(select(Shop).where(
        Shop.id.in_({row.shop_id for row in products.values()})))}
    now, values = database_now(session), []
    for product_id in ids:
        product = products[product_id]
        if str(product_id) in existing or product.last_success_at is None or not product.review_count:
            continue
        shop = shops.get(product.shop_id)
        if (shop is None or shop.platform != product.platform or shop.region != product.region
                or product_id_from_url(product.product_url) != product.platform_product_id):
            raise ValueError("invalid_review_product")
        values.append(_review_task_values(product, shop, now, 1))
    if values:
        session.execute(insert(CrawlTask), values)
    return len(values)


def validate_review_payload(lease):
    payload = lease.payload
    try:
        if (lease.task_type != TaskType.PRODUCT_REVIEWS or lease.platform != "tiktok_shop" or lease.region != "US"
                or not lease.resource_id.isdecimal() or payload["product_id"] != lease.resource_id
                or product_id_from_url(payload["product_url"]) != payload["platform_product_id"]
                or not isinstance(payload["platform_shop_id"], str) or not payload["platform_shop_id"].isdecimal()
                or payload["source_version"] != REVIEW_SOURCE_VERSION
                or str(UUID(payload["root_task_id"])) != payload["root_task_id"]
                or type(payload["page_number"]) is not int or type(payload["max_pages"]) is not int
                or not 1 <= payload["page_number"] <= payload["max_pages"] <= 500
                or type(payload["page_size"]) is not int or payload["page_size"] != 5
                or not isinstance(payload["seen_page_hashes"], list)
                or len(payload["seen_page_hashes"]) != payload["page_number"] - 1
                or any(not isinstance(h, str) or len(h) != 64 or any(c not in "0123456789abcdef" for c in h)
                       for h in payload["seen_page_hashes"])):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise TaskFailure(Failure("invalid_review_task", "评论任务的商品身份或分页参数不一致。")) from None
    return payload


def persist_review_page(session, task, snapshot):
    page, payload = snapshot.page, task.payload
    product = session.scalar(select(Product).where(Product.id == int(task.resource_id),
        Product.platform == task.platform, Product.region == task.region).with_for_update().execution_options(populate_existing=True))
    shop = session.get(Shop, product.shop_id) if product else None
    if (task.task_type != TaskType.PRODUCT_REVIEWS or task.resource_type != ResourceType.PRODUCT
            or not product or not shop or shop.platform_shop_id != payload["platform_shop_id"]
            or product.platform_product_id != page.platform_product_id
            or product.platform_product_id != payload["platform_product_id"]
            or page.page_number != payload["page_number"] or page.page_size != payload["page_size"]):
        raise TaskFailure(Failure("review_scope_conflict", "评论与任务商品或店铺身份不一致，已回滚。"))
    if page.reviews and page.page_hash in payload["seen_page_hashes"]:
        raise TaskFailure(Failure("review_repeated_page", "来源返回已采集的评论页，停止续页以避免循环。"))
    ids = [row.platform_review_id for row in page.reviews]
    existing = {row.platform_review_id: row for row in session.scalars(select(ProductReview).where(
        ProductReview.product_id == product.id, ProductReview.platform_review_id.in_(ids)).with_for_update())}
    new = updated = stale = 0
    for item in page.reviews:
        values = asdict(item)
        values.pop("platform_product_id")
        values["images"] = list(item.images)
        row = existing.get(item.platform_review_id)
        if row and row.last_seen_at > snapshot.captured_at:
            stale += 1
            continue
        if row is None:
            row = ProductReview(product_id=product.id, first_seen_at=snapshot.captured_at, content_version=1)
            session.add(row)
            new += 1
        elif row.content_hash != item.content_hash:
            row.content_version += 1
            updated += 1
        for key, value in values.items():
            setattr(row, key, value)
        row.content_hash = item.content_hash
        row.last_seen_at, row.source_url = snapshot.captured_at, snapshot.source_url
        row.source_kind, row.source_version = page.source_kind, REVIEW_SOURCE_VERSION
        row.response_hash, row.task_id = snapshot.response_hash, task.id
    budget = page.has_more and page.page_number >= payload["max_pages"]
    next_task_id = None
    if page.has_more and not budget:
        next_payload = {**payload, "page_number": page.page_number + 1,
                        "seen_page_hashes": [*payload["seen_page_hashes"], page.page_hash],
                        "source_total": str(page.total) if page.total is not None else None}
        key = fingerprint({"type": "product_reviews", "root": payload["root_task_id"], "page": page.page_number + 1})
        next_task = session.scalar(select(CrawlTask).where(CrawlTask.idempotency_key == key))
        if next_task is None:
            next_task = CrawlTask(platform=task.platform, region=task.region, task_type=TaskType.PRODUCT_REVIEWS,
                resource_type=ResourceType.PRODUCT, resource_id=task.resource_id, idempotency_key=key,
                priority=task.priority, scheduled_for=task.scheduled_for, available_at=database_now(session),
                max_attempts=task.max_attempts, payload=next_payload)
            session.add(next_task)
            session.flush()
        next_task_id = next_task.task_id
    session.flush()
    return {"root_task_id": payload["root_task_id"], "page_number": page.page_number,
            "reviews_seen": len(page.reviews), "reviews_inserted": new, "reviews_updated": updated,
            "stale_reviews_skipped": stale, "source_total": str(page.total) if page.total is not None else None,
            "source_scope": "global", "source_kind": page.source_kind, "has_more": page.has_more,
            "complete": not page.has_more, "next_task_id": next_task_id,
            "stop_reason": "page_budget" if budget else "source_exhausted" if not page.has_more else "page_saved"}
