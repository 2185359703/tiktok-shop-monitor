"""Atomic listing Upsert + detail notifications + the next native page task."""

from sqlalchemy import and_, case, func, not_, select
from sqlalchemy.dialects.mysql import insert

from app.crawlers.product_list import PARSER_VERSION, ProductPageSnapshot
from app.models import CrawlTask, Product, ResourceType, Shop, TaskType
from app.queue.retry_policy import Failure, TaskFailure
from app.services.seed_importer import fingerprint
from app.services.task_runtime import database_now
from app.services.product_images import persist_product_images


LIST_FIELDS = ("title", "currency", "pricing_context", "price_min", "price_max", "original_price_min",
               "original_price_max", "discount_rate", "price_text", "rating", "review_count", "sales_count",
               "sales_count_text", "brand")


def persist_product_page(session, task: CrawlTask, snapshot: ProductPageSnapshot, *, page_number: int,
                         root_task_id: str, max_pages: int) -> dict:
    shop = session.scalar(select(Shop).where(
        Shop.id == int(task.resource_id), Shop.platform == task.platform, Shop.region == task.region,
    ).with_for_update().execution_options(populate_existing=True))
    if (shop is None or task.resource_type != ResourceType.SHOP or shop.platform_shop_id != snapshot.feed.platform_shop_id
            or task.payload.get("platform_shop_id") != shop.platform_shop_id):
        raise TaskFailure(Failure("product_shop_scope_conflict", "商品页与任务店铺归属不一致，已回滚。"))
    candidates = sorted(snapshot.feed.products, key=lambda item: item.platform_product_id)
    identifiers = [item.platform_product_id for item in candidates]
    existing = session.scalars(select(Product).where(
        Product.platform == task.platform, Product.region == task.region, Product.platform_product_id.in_(identifiers),
    ).order_by(Product.platform_product_id).with_for_update().execution_options(populate_existing=True)).all()
    if any(item.shop_id != shop.id for item in existing):
        raise TaskFailure(Failure("product_identity_owner_conflict", "已有商品的平台身份属于其他店铺，已回滚。"))
    existing_ids = {item.platform_product_id for item in existing}
    if candidates:
        rows = []
        for item in candidates:
            values = {field: getattr(item, field) for field in LIST_FIELDS if field != "pricing_context"}
            rows.append({**values, "platform": task.platform, "region": task.region, "shop_id": shop.id,
                         "platform_product_id": item.platform_product_id, "product_url": item.product_url,
                         "pricing_context": "public_listed", "parser_version": PARSER_VERSION,
                         "first_seen_at": snapshot.captured_at, "last_seen_at": snapshot.captured_at})
        statement = insert(Product).values(rows)
        recent = Product.last_seen_at <= statement.inserted.last_seen_at
        listing_owned = and_(recent, Product.last_success_at.is_(None), Product.pricing_context == "public_listed")
        changed = not_(and_(*(getattr(Product, field).op("<=>")(getattr(statement.inserted, field)) for field in LIST_FIELDS)))
        updates = {field: case((listing_owned, getattr(statement.inserted, field)), else_=getattr(Product, field)) for field in LIST_FIELDS}
        updates.update(
            product_url=case((recent, statement.inserted.product_url), else_=Product.product_url),
            parser_version=case((listing_owned, statement.inserted.parser_version), else_=Product.parser_version),
            data_version=case((and_(listing_owned, changed), Product.data_version + 1), else_=Product.data_version),
            first_seen_at=func.least(Product.first_seen_at, statement.inserted.first_seen_at),
            last_seen_at=func.greatest(Product.last_seen_at, statement.inserted.last_seen_at),
        )
        # A discovery page never erases detail-only data, stock, hashes or snapshots.
        # MySQL evaluates assignments left-to-right. Compare before replacing fields.
        ordered = [("data_version", updates.pop("data_version")), *updates.items()]
        session.execute(statement.on_duplicate_key_update(ordered))
    products = session.scalars(select(Product).where(
        Product.platform == task.platform, Product.region == task.region, Product.platform_product_id.in_(identifiers),
    ).order_by(Product.platform_product_id).execution_options(populate_existing=True)).all()
    now = database_now(session)
    detail_rows, detail_keys = [], []
    images_by_id = {item.platform_product_id: item.images for item in candidates}
    images_upserted = 0
    for product in products:
        images_upserted += persist_product_images(session, product.id, images_by_id[product.platform_product_id],
                                                 source_url=snapshot.final_url, source_kind="listing",
                                                 observed_at=snapshot.captured_at)
        key = fingerprint({"platform": task.platform, "region": task.region, "task_type": "product_detail",
                           "platform_product_id": product.platform_product_id, "root_task_id": root_task_id})
        detail_keys.append(key)
        detail_rows.append({"platform": task.platform, "region": task.region, "task_type": TaskType.PRODUCT_DETAIL,
                            "resource_type": ResourceType.PRODUCT, "resource_id": str(product.id),
                            "idempotency_key": key, "priority": 3, "scheduled_for": task.scheduled_for, "available_at": now,
                            "payload": {"schema_version": 1, "product_id": str(product.id),
                                "platform_product_id": product.platform_product_id, "product_url": product.product_url,
                                "shop_id": str(shop.id), "platform_shop_id": shop.platform_shop_id,
                                "root_task_id": root_task_id, "discovery_task_id": task.task_id,
                                "source_response_hash": snapshot.response_hash, "pricing_context": "public_listed"}})
    previous_keys = set(session.scalars(select(CrawlTask.idempotency_key).where(CrawlTask.idempotency_key.in_(detail_keys))))
    if detail_rows:
        detail_statement = insert(CrawlTask).values(detail_rows)
        session.execute(detail_statement.on_duplicate_key_update(idempotency_key=detail_statement.inserted.idempotency_key))
    next_task_created = False
    descendant_keys = list(detail_keys)
    budget_reached = snapshot.feed.has_more and page_number >= max_pages
    if snapshot.feed.has_more and not budget_reached:
        continuation = snapshot.feed.next_pagination
        key = fingerprint({"platform": task.platform, "region": task.region, "task_type": "product_discovery",
                           "platform_shop_id": shop.platform_shop_id, "root_task_id": root_task_id,
                           "page_number": page_number + 1, "cursor": continuation.search_params})
        descendant_keys.append(key)
        if session.scalar(select(CrawlTask.id).where(CrawlTask.idempotency_key == key).with_for_update()) is None:
            session.add(CrawlTask(
                platform=task.platform, region=task.region, task_type=TaskType.PRODUCT_DISCOVERY,
                resource_type=ResourceType.SHOP, resource_id=str(shop.id), priority=task.priority,
                idempotency_key=key, scheduled_for=task.scheduled_for, available_at=now,
                payload={"schema_version": 2, "shop_id": str(shop.id), "platform_shop_id": shop.platform_shop_id,
                         "shop_url": shop.canonical_url, "root_task_id": root_task_id, "parent_task_id": task.task_id,
                         "page_number": page_number + 1, "max_pages": max_pages,
                         "public_pagination": continuation.to_payload()},
            ))
            next_task_created = True
    if (snapshot.feed.total_product_count is not None
            and (shop.product_count_captured_at or shop.last_seen_at) <= snapshot.captured_at):
        shop.product_count = snapshot.feed.total_product_count
        shop.product_count_text = str(snapshot.feed.total_product_count)
        shop.product_count_captured_at = snapshot.captured_at
        if shop.profile_field_state is not None:
            shop.profile_field_state = {**shop.profile_field_state, "product_count": {
                "state": "exact", "source": "shop_info.component_data.shopInfo.on_sell_product_count",
                "scope": "platform_on_sell_product_count", "captured_at": snapshot.captured_at.isoformat(),
            }}
    if shop.last_seen_at <= snapshot.captured_at:
        shop.last_seen_at = snapshot.captured_at
    session.flush()
    if (task.payload or {}).get("collection_tracked"):
        from app.services.collection_intake import inherit_launch_tasks
        children = session.scalars(select(CrawlTask).where(CrawlTask.idempotency_key.in_(descendant_keys))
            .order_by(CrawlTask.id).with_for_update()).all()
        inherit_launch_tasks(session, task, children)
    from app.services.analytics_refresh import mark_products
    mark_products(session, [product.id for product in products])
    return {"new_products": len(set(identifiers) - existing_ids), "page_products": len(identifiers),
            "images_upserted": images_upserted,
            "new_detail_tasks": len(detail_keys) - len(previous_keys), "next_task_created": next_task_created,
            "root_task_id": root_task_id, "page_number": page_number, "has_more": snapshot.feed.has_more,
            "complete": not snapshot.feed.has_more, "stop_reason": "page_budget" if budget_reached else
            ("source_exhausted" if not snapshot.feed.has_more else "page_saved")}
