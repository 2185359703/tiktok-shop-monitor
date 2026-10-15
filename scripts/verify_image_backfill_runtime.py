"""Read-only before/after evidence for real image-only worker deliveries."""

import argparse
import hashlib
import json
from pathlib import Path

from sqlalchemy import exists, func, select

from app.crawlers.product_images import IMAGE_SOURCE_VERSION
from app.models import CrawlTask, Product, ProductVariant, TaskStatus, TaskType
from app.models.product_image import ProductImage
from app.services.image_backfill import BACKFILL_MODES, capture_mode_expression
from app.models.product_content import ProductContent
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory


FIELDS = ("title", "price_min", "price_max", "discount_rate", "stock_status", "content_hash", "data_version",
          "last_seen_at", "last_success_at", "parser_version", "description", "raw_attributes")


def signature(session, product):
    columns = [ProductVariant.id, ProductVariant.platform_variant_id, ProductVariant.variant_key,
               ProductVariant.price, ProductVariant.original_price, ProductVariant.stock_status,
               ProductVariant.updated_at, ProductVariant.options, ProductVariant.available_quantity,
               ProductVariant.variant_payload_hash]
    digest = hashlib.sha256()
    digest.update(json.dumps({key: getattr(product, key) for key in FIELDS}, sort_keys=True, default=str).encode())
    count = 0
    for row in session.execute(select(*columns).where(ProductVariant.product_id == product.id)
                               .order_by(ProductVariant.id)).yield_per(100):
        digest.update(json.dumps(list(row), default=str).encode())
        count += 1
    return {"product_id": str(product.id), "business_signature": digest.hexdigest(), "variant_count": count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    config = Settings.load()
    engine = make_engine(config.database_url)
    try:
        with make_session_factory(engine)() as session:
            if args.baseline is None:
                covered = exists().where(ProductImage.product_id == Product.id, ProductImage.is_current.is_(True))
                products = session.scalars(select(Product).where(Product.platform == config.platform,
                    Product.region == config.region, Product.currency == config.currency,
                    Product.pricing_context == config.pricing_context, Product.last_success_at.is_not(None), ~covered)
                    .order_by(Product.id).limit(100))
                data = {"business_data_read_only": True, "sample_limit": 100,
                        "items": [signature(session, product) for product in products]}
            else:
                baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
                items = []
                for before in baseline["items"]:
                    product = session.get(Product, int(before["product_id"]))
                    completed = session.scalar(select(func.count()).select_from(CrawlTask).where(
                        CrawlTask.platform == config.platform, CrawlTask.region == config.region,
                        CrawlTask.task_type == TaskType.PRODUCT_DETAIL, CrawlTask.resource_id == str(product.id),
                        capture_mode_expression().in_(BACKFILL_MODES),
                        CrawlTask.payload["image_source_version"].as_string() == IMAGE_SOURCE_VERSION,
                        CrawlTask.status == TaskStatus.SUCCEEDED))
                    if not completed:
                        continue
                    after = signature(session, product)
                    count = int(session.scalar(select(func.count()).select_from(ProductImage).where(
                        ProductImage.product_id == product.id, ProductImage.is_current.is_(True))) or 0)
                    items.append({"product_id": str(product.id), "current_images": count,
                                  "content_captured": bool(session.scalar(select(exists().where(ProductContent.product_id == product.id)))),
                                  "business_unchanged": before == after})
                data = {"business_data_read_only": True, "verified_completed_image_tasks": len(items), "items": items,
                        "all_business_unchanged": all(row["business_unchanged"] for row in items) if items else None}
                if items and not data["all_business_unchanged"]:
                    print(json.dumps(data, ensure_ascii=False, indent=2))
                    raise SystemExit(1)
            print(json.dumps(data, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
