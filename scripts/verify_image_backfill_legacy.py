"""Compare the existing v1 baseline using its original field projection.

The baseline was captured by frontend-20261015-images-93e6b420. Later tooling
added description/raw_attributes and extra SKU columns, so its v2 digest cannot
be compared to that saved v1 digest. This read-only adapter preserves the exact
original algorithm while the current verifier recognizes media task outcomes.
"""

import hashlib
import json

from sqlalchemy import select

from app.models import ProductVariant
from scripts import verify_image_backfill_runtime as verifier


FIELDS = ("title", "price_min", "price_max", "discount_rate", "stock_status", "content_hash", "data_version",
          "last_seen_at", "last_success_at", "parser_version")


def original_signature(session, product):
    columns = [ProductVariant.id, ProductVariant.platform_variant_id, ProductVariant.variant_key,
               ProductVariant.price, ProductVariant.original_price, ProductVariant.stock_status,
               ProductVariant.updated_at]
    digest = hashlib.sha256()
    digest.update(json.dumps({key: getattr(product, key) for key in FIELDS}, sort_keys=True, default=str).encode())
    count = 0
    for row in session.execute(select(*columns).where(ProductVariant.product_id == product.id)
                               .order_by(ProductVariant.id)).yield_per(100):
        digest.update(json.dumps(list(row), default=str).encode())
        count += 1
    return {"product_id": str(product.id), "business_signature": digest.hexdigest(), "variant_count": count}


if __name__ == "__main__":
    verifier.signature = original_signature
    verifier.main()
