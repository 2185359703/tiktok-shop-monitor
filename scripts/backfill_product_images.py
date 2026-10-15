"""Explicit, bounded photo backfill; preview is the default, with no HTTP or writes."""

import argparse
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.tiktok_shop import TikTokShopAdapter
from app.crawlers.public_shop import PublicPageError
from app.models import Product, Shop
from app.queue.redis_gate import RedisGate
from app.settings import Settings
from app.services.product_images import images_table_available, persist_product_images
from app.storage.database import make_engine
from scripts.runtime_support import stop_signal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product-id", action="append", required=True, type=int,
                        help="Internal product ID; repeat for at most 20 explicitly chosen products.")
    parser.add_argument("--execute", action="store_true", help="Fetch and save images after the additive migration.")
    args = parser.parse_args()
    ids = list(dict.fromkeys(args.product_id))
    if not 1 <= len(ids) <= 20 or any(value <= 0 for value in ids):
        raise ValueError("每次仅能补采 1-20 个明确指定的商品内部 ID。")
    settings = Settings.load()
    engine = make_engine(settings.database_url)
    gate = None
    try:
        with Session(engine) as session:
            rows = session.execute(select(Product.id, Product.shop_id, Product.product_url, Product.platform_product_id,
                                          Shop.platform_shop_id)
                                   .join(Shop, Product.shop_id == Shop.id)
                                   .where(Product.id.in_(ids), Product.platform == settings.platform,
                                          Product.region == settings.region)).all()
            if {row.id for row in rows} != set(ids):
                raise ValueError("部分指定商品不存在或不属于当前市场。")
            if not args.execute:
                print(json.dumps({"mode": "dry_run", "products": [str(row.id) for row in rows],
                                  "http_requests": 0, "writes": 0, "schema_ready": images_table_available(session)},
                                 ensure_ascii=False))
                return 0
            if not images_table_available(session):
                raise ValueError("需先发布 003_product_images.sql 图片扩展表。")
        gate = RedisGate(settings)
        stop = stop_signal()
        adapter = TikTokShopAdapter(gate, settings)
        for row in rows:
            if stop.is_set():
                break
            try:
                snapshot = adapter.fetch_product_detail(row.product_url, row.platform_shop_id, stop)
            except PublicPageError as error:
                if error.status in {401, 403, 429} or error.code == "access_limited":
                    gate.pause()
                print(json.dumps({"product_id": str(row.id), "status": "stopped", "code": error.code}))
                return 1
            with Session(engine) as session, session.begin():
                product = session.scalar(select(Product).where(Product.id == row.id).with_for_update())
                if (product is None or product.platform_product_id != snapshot.detail.platform_product_id
                        or product.shop_id != row.shop_id or product.platform != settings.platform
                        or product.region != settings.region):
                    raise ValueError("商品身份在补采期间变化，禁止写入。")
                count = persist_product_images(session, product.id, snapshot.detail.images,
                                               source_url=snapshot.final_url, source_kind="detail",
                                               observed_at=snapshot.captured_at)
            print(json.dumps({"product_id": str(row.id), "images_upserted": count,
                              "status": "available" if count else "not_captured"}))
        return 0
    finally:
        if gate is not None:
            gate.close()
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # Connection strings and raw response bodies must never be printed.
        print(json.dumps({"error_type": type(error).__name__, "message": "图片补采未完成。"}, ensure_ascii=False))
        raise SystemExit(1)
