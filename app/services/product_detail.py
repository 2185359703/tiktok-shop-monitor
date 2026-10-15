"""Lease-protected detail enrichment and SKU upserts, without deleting history."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert

from app.crawlers.product_detail import PARSER_VERSION
from app.models import AvailabilityStatus, Product, ProductVariant, ResourceType, Shop
from app.queue.retry_policy import Failure, TaskFailure
from app.services.seed_importer import fingerprint
from app.models import CrawlTask, TaskType
from app.services.task_runtime import database_now
from app.services.product_images import persist_product_images
from app.services.image_backfill import capture_mode_expression
from app.services.product_content import persist_product_content


DETAIL_FIELDS = ("title", "original_category", "color_summary", "size_summary", "currency", "price_text", "price_min", "price_max",
                 "original_price_min", "original_price_max", "discount_rate", "stock_status", "rating", "review_count", "sales_count", "sales_count_text")


def persist_product_detail(session, task, snapshot):
    product = session.scalar(select(Product).where(Product.id == int(task.resource_id), Product.platform == task.platform,
                                                   Product.region == task.region).with_for_update().execution_options(populate_existing=True))
    detail = snapshot.detail
    shop = session.get(Shop, product.shop_id) if product is not None else None
    if (product is None or shop is None or task.resource_type != ResourceType.PRODUCT
            or detail.platform_product_id != product.platform_product_id or detail.platform_shop_id != shop.platform_shop_id
            or task.payload.get("platform_product_id") != product.platform_product_id
            or task.payload.get("platform_shop_id") != shop.platform_shop_id):
        raise TaskFailure(Failure("detail_resource_scope_conflict", "详情、SKU 与已有商品归属不一致，已回滚。"))
    if product.pricing_context != "public_listed" or product.currency != "USD":
        raise TaskFailure(Failure("detail_pricing_context_conflict", "公开详情不能覆盖其他价格上下文。"))
    if product.last_seen_at > snapshot.captured_at or (product.last_success_at is not None and product.last_success_at > snapshot.captured_at):
        return {"detail_updated": False, "stale_observation_skipped": True, "variants_seen": len(detail.variants), "variants_upserted": 0}
    retained = []
    for field in DETAIL_FIELDS:
        setattr(product, field, getattr(detail, field))
    if detail.description is not None and not detail.raw_attributes["unsupported_description_blocks"]:
        product.description = detail.description
    elif product.description is not None:
        retained.append("description")
    for field, value in detail.attributes.items():
        if (value is not None or field in detail.raw_attributes["attribute_conflicts"]
                or field in detail.raw_attributes.get("normalization_issues", {})):
            setattr(product, field, value)
        elif getattr(product, field) is not None:
            retained.append(field)
    product.product_url, product.availability_status = detail.product_url, AvailabilityStatus.AVAILABLE
    # Stable content version excludes changing price/stock/sales/review metrics.
    content = {"product_id": product.platform_product_id, "title": product.title, "description": product.description,
               "original_category": product.original_category, "attributes": {name: getattr(product, name) for name in detail.attributes},
               "color_summary": detail.color_summary, "size_summary": detail.size_summary,
               "variants": [{"id": value.platform_variant_id, "options": value.options} for value in detail.variants]}
    content_hash = fingerprint(content)
    if product.content_hash != content_hash:
        product.data_version += 1
    product.content_hash, product.parser_version = content_hash, PARSER_VERSION
    product.raw_attributes = {**detail.raw_attributes, "retained_attributes": retained,
                              "observed_sku_ids": [value.platform_variant_id for value in detail.variants]}
    product.last_seen_at = product.last_success_at = snapshot.captured_at
    old_variants = session.scalars(select(ProductVariant).where(ProductVariant.product_id == product.id)
                                  .order_by(ProductVariant.id).with_for_update()).all()
    by_id = {row.platform_variant_id: row for row in old_variants if row.platform_variant_id is not None}
    by_key = {row.variant_key: row for row in old_variants}
    rows = []
    fields = ("platform_variant_id", "options", "color", "size", "material", "currency", "price", "original_price",
              "stock_status", "available_quantity", "variant_payload_hash")
    for variant in detail.variants:
        previous = by_id.get(variant.platform_variant_id)
        key = previous.variant_key if previous is not None else variant.variant_key
        if key in by_key and by_key[key].platform_variant_id != variant.platform_variant_id:
            raise TaskFailure(Failure("variant_identity_conflict", "SKU 唯一键与已有平台身份冲突，已回滚。"))
        rows.append({"product_id": product.id, "variant_key": key,
                     **{field: getattr(variant, field) for field in fields}})
    statement = insert(ProductVariant).values(rows)
    session.execute(statement.on_duplicate_key_update(**{field: getattr(statement.inserted, field) for field in fields}))
    # Missing variants are not deleted or automatically treated as removed.
    images_upserted = persist_product_images(session, product.id, detail.images,
                                             source_url=snapshot.final_url, source_kind="detail",
                                             observed_at=snapshot.captured_at)
    content_summary = persist_product_content(session, product.id, detail.content,
        source_url=snapshot.final_url, response_hash=snapshot.response_hash, observed_at=snapshot.captured_at) if detail.content is not None else {}
    session.flush()
    return {"detail_updated": True, "stale_observation_skipped": False, "variants_seen": len(rows),
            "variants_upserted": len(rows), "new_variants": sum(value.platform_variant_id not in by_id for value in detail.variants),
            "attribute_conflicts": len(detail.raw_attributes["attribute_conflicts"]), "content_hash": content_hash,
            "images_upserted": images_upserted, **content_summary}


def ensure_initial_detail_task(session, product, shop):
    existing = session.scalar(select(CrawlTask).where(
        CrawlTask.platform == product.platform, CrawlTask.region == product.region,
        CrawlTask.task_type == TaskType.PRODUCT_DETAIL, CrawlTask.resource_type == ResourceType.PRODUCT,
        capture_mode_expression() == "full",
        CrawlTask.resource_id == str(product.id),
    ).order_by(CrawlTask.id.desc()).limit(1).with_for_update())
    if existing is not None: return existing, False
    now = database_now(session)
    key = fingerprint({"platform": product.platform, "region": product.region, "task_type": "product_detail",
                       "platform_product_id": product.platform_product_id, "context": "initial_detail_backfill"})
    task = CrawlTask(platform=product.platform, region=product.region, task_type=TaskType.PRODUCT_DETAIL,
                     resource_type=ResourceType.PRODUCT, resource_id=str(product.id), idempotency_key=key,
                     priority=3, scheduled_for=now, available_at=now,
                     payload={"schema_version": 1, "product_id": str(product.id), "platform_product_id": product.platform_product_id,
                              "product_url": product.product_url, "shop_id": str(shop.id), "platform_shop_id": shop.platform_shop_id,
                              "pricing_context": "public_listed"})
    session.add(task)
    session.flush()
    return task, True
