"""Fill missing initial review jobs for the existing verified US clothing shops."""

import argparse
import asyncio
from collections import defaultdict, deque
import json
from pathlib import Path
import tomllib

from sqlalchemy import String, cast, exists, select

from app.models import CrawlTask, Product, ResourceType, Shop, TaskType
from app.services.product_reviews import enqueue_initial_review_tasks, reviews_available
from app.settings import Settings
from app.storage.async_database import AsyncDatabase, make_async_engine
from app.storage.database import PROJECT_ROOT


def candidates(session, settings, shop_ids):
    if TaskType.PRODUCT_REVIEWS not in settings.enabled_task_types:
        raise ValueError("review_collection_disabled")
    if not reviews_available(session):
        raise ValueError("review_schema_not_ready")
    shops = session.scalars(select(Shop.id).where(
        Shop.platform == settings.platform, Shop.region == settings.region,
        Shop.platform_shop_id.in_(shop_ids),
    )).all()
    if len(shops) != len(shop_ids):
        raise ValueError("verified_shop_scope_incomplete")
    scheduled = exists(select(CrawlTask.id).where(
        CrawlTask.platform == Product.platform, CrawlTask.region == Product.region,
        CrawlTask.task_type == TaskType.PRODUCT_REVIEWS, CrawlTask.resource_type == ResourceType.PRODUCT,
        CrawlTask.resource_id == cast(Product.id, String),
    ))
    rows = session.execute(select(Product.id, Product.shop_id).where(
        Product.platform == settings.platform, Product.region == settings.region,
        Product.shop_id.in_(shops), Product.last_success_at.is_not(None),
        Product.review_count > 0, ~scheduled,
    ).order_by(Product.id)).all()
    # Spread early results across shops instead of making a large shop consume
    # every slot before the other monitored shops receive any reviews.
    buckets = defaultdict(deque)
    for product_id, shop_id in rows:
        buckets[shop_id].append(product_id)
    ordered = []
    while buckets:
        for shop_id in list(buckets):
            ordered.append(buckets[shop_id].popleft())
            if not buckets[shop_id]:
                del buckets[shop_id]
    return ordered


def enqueue_batch(session, settings, product_ids):
    return enqueue_initial_review_tasks(session, settings, product_ids)


async def run(args):
    settings = Settings.load()
    config = tomllib.loads(args.scope_file.read_text(encoding="utf-8"))
    shop_ids = config["runtime"]["shop_profile_shop_ids"]
    if (settings.platform != "tiktok_shop" or settings.region != "US"
            or len(shop_ids) != 100 or len(set(shop_ids)) != 100
            or any(not isinstance(value, str) or not value.isdecimal() for value in shop_ids)):
        raise ValueError("expected_100_verified_us_shops")
    database = AsyncDatabase(make_async_engine(settings.database_url))
    try:
        products = await database.read(candidates, settings, shop_ids)
        selected = products[:args.limit] if args.limit else products
        created = 0
        if args.execute:
            for offset in range(0, len(selected), args.batch_size):
                created += await database.transaction(enqueue_batch, settings, selected[offset:offset + args.batch_size])
                print(json.dumps({"phase": "enqueue", "created_tasks": created,
                                  "processed": min(offset + args.batch_size, len(selected)),
                                  "selected": len(selected)}), flush=True)
        result = {"phase": "complete", "execute": args.execute, "scope_shops": len(shop_ids),
                  "eligible_products": len(products), "selected_products": len(selected),
                  "created_tasks": created, "max_pages": 1,
                  "requests_executed_by_existing_workers": bool(created)}
        print(json.dumps(result), flush=True)
        return result
    finally:
        await database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--scope-file", type=Path, default=PROJECT_ROOT / "configs/shop-profiles.toml")
    parser.add_argument("--limit", type=int, default=0, help="Maximum products; 0 selects all missing jobs.")
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()
    if args.limit < 0 or not 1 <= args.batch_size <= 100:
        parser.error("limit must be nonnegative and batch-size must be 1-100")
    asyncio.run(run(args))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # SQL/connection errors can contain connection information. Keep those
        # out of operator-facing output and deployment records.
        print(json.dumps({"error_type": type(error).__name__, "message": "Review backfill did not complete."}))
        raise SystemExit(1)
