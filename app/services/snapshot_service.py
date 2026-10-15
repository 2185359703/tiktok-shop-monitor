"""Immutable observations, window-idempotent tasks and bounded periodic scheduling."""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.mysql import insert

from app.crawlers.product_detail import PARSER_VERSION
from app.crawlers.product_list import product_id_from_url
from app.models import AvailabilityStatus, CrawlTask, Product, ProductSnapshot, ProductVariant, ResourceType, Shop, TaskType, VariantSnapshot
from app.queue.retry_policy import Failure, TaskFailure
from app.services.product_detail import persist_product_detail
from app.services.seed_importer import fingerprint
from app.services.task_runtime import database_now


SNAPSHOT_FORMAT = 1
SNAPSHOT_FIELDS = ("currency", "price_text", "price_min", "price_max", "original_price_min", "original_price_max",
                   "discount_rate", "stock_status", "sales_count", "sales_count_text", "rating", "review_count")


def utc_window(value: datetime, seconds: int) -> datetime:
    if value.tzinfo is None:
        raise ValueError("快照窗口必须包含时区。")
    whole = int(value.timestamp())
    return datetime.fromtimestamp(whole - whole % seconds, timezone.utc)


def snapshot_task_key(product, window):
    return fingerprint({"platform": product.platform, "region": product.region, "task_type": "product_snapshot",
                        "platform_product_id": product.platform_product_id, "scheduled_for": window.isoformat(),
                        "pricing_context": "public_listed"})


def ensure_snapshot_task(session, product, window):
    if window.tzinfo is None:
        raise ValueError("快照窗口必须包含时区。")
    window = window.astimezone(timezone.utc)
    if product.last_success_at is None or product.pricing_context != "public_listed" or product.currency != "USD":
        raise ValueError("周期快照仅接受已补全的美国站公开标价商品。")
    if product_id_from_url(product.product_url) != product.platform_product_id:
        raise ValueError("商品公开链接与平台身份不一致。")
    shop = session.get(Shop, product.shop_id)
    key = snapshot_task_key(product, window)
    previous = session.scalar(select(CrawlTask).where(CrawlTask.idempotency_key == key).with_for_update())
    if previous is not None:
        return previous, False
    task = CrawlTask(platform=product.platform, region=product.region, task_type=TaskType.PRODUCT_SNAPSHOT,
                     resource_type=ResourceType.PRODUCT, resource_id=str(product.id), idempotency_key=key, priority=2,
                     scheduled_for=window, available_at=max(window, database_now(session)),
                     payload={"schema_version": 1, "product_id": str(product.id), "platform_product_id": product.platform_product_id,
                              "product_url": product.product_url, "shop_id": str(shop.id), "platform_shop_id": shop.platform_shop_id,
                              "pricing_context": "public_listed"})
    session.add(task)
    session.flush()
    return task, True


def ensure_change_task(session, product, snapshot):
    key = fingerprint({"platform": product.platform, "region": product.region, "task_type": "change_detection",
                       "snapshot_id": str(snapshot.id), "rule_version": 1})
    previous = session.scalar(select(CrawlTask).where(CrawlTask.idempotency_key == key).with_for_update())
    if previous is not None:
        return previous, False
    now = database_now(session)
    task = CrawlTask(platform=product.platform, region=product.region, task_type=TaskType.CHANGE_DETECTION,
                     resource_type=ResourceType.PRODUCT, resource_id=str(product.id), idempotency_key=key, priority=6,
                     scheduled_for=snapshot.scheduled_for, available_at=now,
                     payload={"schema_version": 1, "product_id": str(product.id), "snapshot_id": str(snapshot.id),
                              "pricing_context": snapshot.pricing_context})
    session.add(task)
    session.flush()
    return task, True


def detail_observation(detail):
    value = asdict(detail)
    return normalize_json(value)


def normalize_json(value):
    if isinstance(value, Decimal): return str(value)
    if isinstance(value, datetime): return value.isoformat()
    if isinstance(value, dict): return {key: normalize_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)): return [normalize_json(item) for item in value]
    return value


def observed_content(detail):
    return {"title": detail.title, "description": detail.description, "original_category": detail.original_category,
            "attributes": detail.attributes, "colors": detail.color_summary, "sizes": detail.size_summary,
            "variant_options": {item.platform_variant_id: item.options for item in detail.variants}}


def persist_snapshot(session, task, response, *, update_current=True, interval_seconds=3600):
    product = session.scalar(select(Product).where(Product.id == int(task.resource_id), Product.platform == task.platform,
                                                   Product.region == task.region).with_for_update().execution_options(populate_existing=True))
    detail = response.detail
    shop = session.get(Shop, product.shop_id) if product is not None else None
    if (product is None or shop is None or task.resource_type != ResourceType.PRODUCT
            or detail.platform_product_id != product.platform_product_id or detail.platform_shop_id != shop.platform_shop_id
            or task.payload.get("platform_product_id") != product.platform_product_id
            or task.payload.get("platform_shop_id") != shop.platform_shop_id):
        raise TaskFailure(Failure("snapshot_identity_conflict", "快照与任务商品身份或归属不一致。"))
    if (response.status != 200 or task.payload.get("pricing_context", "public_listed") != "public_listed"
            or product.pricing_context != "public_listed" or detail.currency != "USD"):
        raise TaskFailure(Failure("snapshot_context_conflict", "快照价格上下文或来源状态不一致。"))
    if product.currency != "USD":
        raise TaskFailure(Failure("snapshot_context_conflict", "快照不能覆盖其他币种的当前商品。"))
    existing = session.scalar(select(ProductSnapshot).where(
        ProductSnapshot.product_id == product.id, ProductSnapshot.scheduled_for == task.scheduled_for,
        ProductSnapshot.pricing_context == "public_listed",
    ).with_for_update())
    if existing is not None:
        # First valid observation in a logical window is immutable, even if a replay differs.
        _, created = ensure_change_task(session, product, existing)
        return {"snapshot_id": str(existing.id), "snapshots_written": 0, "variant_snapshots_written": 0,
                "change_task_created": created, "window_already_saved": True}
    latest = session.scalar(select(ProductSnapshot).where(ProductSnapshot.product_id == product.id,
                              ProductSnapshot.pricing_context == "public_listed").order_by(
                              ProductSnapshot.captured_at.desc(), ProductSnapshot.id.desc()).limit(1))
    if latest is not None and response.captured_at <= latest.captured_at:
        raise TaskFailure(Failure("out_of_order_snapshot", "已有更晚的快照；拒绝插入倒序观察，不改写事件历史。"))
    if response.captured_at < task.scheduled_for:
        raise TaskFailure(Failure("snapshot_before_window", "实际观察时间早于逻辑采集窗口。"))
    if update_current:
        persist_product_detail(session, task, response)
    variants = session.scalars(select(ProductVariant).where(ProductVariant.product_id == product.id)
                               .with_for_update()).all()
    by_id = {item.platform_variant_id: item for item in variants}
    if any(item.platform_variant_id not in by_id for item in detail.variants):
        raise TaskFailure(Failure("snapshot_variant_missing", "快照 SKU 尚未完成同事务入库。"))
    raw = {"snapshot_format": SNAPSHOT_FORMAT, "response_hash": response.response_hash,
           "observation": detail_observation(detail), "observed_content": observed_content(detail),
           "source": "live_public_pdp" if update_current else "successful_detail_evidence"}
    snapshot = ProductSnapshot(product_id=product.id, task_id=task.id, scheduled_for=task.scheduled_for,
                               captured_at=response.captured_at, pricing_context="public_listed",
                               **{field: getattr(detail, field) for field in SNAPSHOT_FIELDS},
                               availability_status=AvailabilityStatus.AVAILABLE, variant_count=len(detail.variants),
                               content_hash=fingerprint(raw["observed_content"]), parser_version=PARSER_VERSION,
                               source_status=200, source_url=response.final_url, raw_payload=raw)
    session.add(snapshot)
    session.flush()
    rows = [{"product_snapshot_id": snapshot.id, "product_id": product.id,
             "variant_id": by_id[value.platform_variant_id].id, "captured_at": response.captured_at,
             **{field: getattr(value, field) for field in ("currency", "price", "original_price", "stock_status", "available_quantity", "variant_payload_hash")}}
            for value in detail.variants]
    session.execute(insert(VariantSnapshot).values(rows))
    _, created = ensure_change_task(session, product, snapshot)
    if update_current and product.last_success_at == response.captured_at:
        product.next_capture_at = utc_window(response.captured_at, interval_seconds) + timedelta(seconds=interval_seconds)
    session.flush()
    return {"snapshot_id": str(snapshot.id), "snapshots_written": 1, "variant_snapshots_written": len(rows),
            "change_task_created": created, "window_already_saved": False}


def enqueue_due_snapshots(session, settings, *, limit=100):
    if not settings.periodic_snapshots_enabled:
        return 0
    now = database_now(session)
    rows = session.scalars(select(Product).where(
        Product.platform == settings.platform, Product.region == settings.region,
        Product.platform_product_id.in_(settings.monitored_product_ids), Product.last_success_at.is_not(None),
        Product.pricing_context == "public_listed", Product.currency == "USD",
        or_(Product.next_capture_at.is_(None), Product.next_capture_at <= now),
    ).order_by(Product.id).limit(limit).with_for_update(skip_locked=True)).all()
    created = 0
    for product in rows:
        _, inserted = ensure_snapshot_task(session, product, utc_window(now, settings.snapshot_interval_seconds))
        created += inserted
    return created
