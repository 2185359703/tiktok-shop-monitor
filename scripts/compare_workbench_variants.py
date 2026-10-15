"""Compare old/new current-SKU aggregation in one read-only snapshot."""

import argparse
import json
import time

from sqlalchemy import and_, cast, func, literal_column, select, text, true
from sqlalchemy.dialects.mysql import BINARY

from app.models import Product, ProductVariant, Shop
from app.services.analytics_service import current_variant_condition, filters, observed_variants_query, products_query
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory


config = Settings.load()
parser = argparse.ArgumentParser()
parser.add_argument("--shop-id", type=int)
parser.add_argument("--explain-only", action="store_true")
parser.add_argument("--projection", action="store_true")
parser.add_argument("--force-primary", action="store_true")
parser.add_argument("--aggregate-only", action="store_true")
parser.add_argument("--inspect-sizes", action="store_true")
parser.add_argument("--variant-first", action="store_true")
args = parser.parse_args()
engine = make_engine(config.database_url)
stage = "connect"
try:
    with make_session_factory(engine)() as db:
        db.execute(text("SET SESSION MAX_EXECUTION_TIME=30000"))
        if args.inspect_sizes:
            sizes = db.execute(select(Product.id, func.json_storage_size(Product.raw_attributes),
                func.json_length(func.json_extract(Product.raw_attributes, "$.observed_sku_ids")))
                .where(Product.last_success_at.is_not(None)).limit(10)).all()
            print(json.dumps({"sizes": [list(row) for row in sizes], "tables": [list(row) for row in db.execute(text(
                "SELECT table_name, data_length, index_length FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name IN ('products','product_variants')"))]}, default=str), flush=True)
            raise SystemExit(0)
        clauses, scope = filters(config, session=db, shop_id=args.shop_id)
        old = products_query(ProductVariant.stock_status, func.count()).join(
            ProductVariant, ProductVariant.product_id == Product.id
        ).where(*clauses, ProductVariant.currency == scope["currency"], current_variant_condition(db)).group_by(ProductVariant.stock_status)
        new = observed_variants_query(db, clauses, scope["currency"], ProductVariant.stock_status, func.count())\
            .group_by(ProductVariant.stock_status)
        if args.projection:
            values = func.json_table(Product.raw_attributes, literal_column(
                "'$.observed_sku_ids[*]' COLUMNS (sku_id VARCHAR(128) PATH '$' NULL ON ERROR, raw_sku JSON PATH '$' NULL ON ERROR)"
            )).table_valued("sku_id", "raw_sku", joins_implicitly=True)
            new = products_query(ProductVariant.id).join(values, true()).join(ProductVariant, and_(
                ProductVariant.product_id == Product.id,
                ProductVariant.platform_variant_id == values.c.sku_id.collate("utf8mb4_bin")
            )).where(*clauses, ProductVariant.currency == scope["currency"], func.json_type(values.c.raw_sku) == "STRING",
                cast(ProductVariant.platform_variant_id, BINARY) == cast(func.json_unquote(values.c.raw_sku), BINARY))\
                .prefix_with("STRAIGHT_JOIN", dialect="mysql")
        if args.force_primary:
            new = new.with_hint(Product, "FORCE INDEX (PRIMARY)", "mysql")
        if args.variant_first:
            new = select(ProductVariant.stock_status, func.count()).select_from(ProductVariant).join(
                Product, Product.id == ProductVariant.product_id).join(Shop, Shop.id == Product.shop_id).where(
                *clauses, ProductVariant.currency == scope["currency"], current_variant_condition(db))\
                .prefix_with("STRAIGHT_JOIN", dialect="mysql").group_by(ProductVariant.stock_status)
        if args.aggregate_only:
            new = new.with_only_columns(ProductVariant.stock_status, func.count()).group_by(ProductVariant.stock_status)
        stage = "explain"
        plan = db.execute(text("EXPLAIN " + str(new.compile(dialect=engine.dialect, compile_kwargs={"literal_binds": True})))).mappings().all()
        print(json.dumps({"stage": "new_query_plan", "explain": [dict(row) for row in plan]}, ensure_ascii=False, default=str), flush=True)
        if args.explain_only:
            raise SystemExit(0)
        if args.aggregate_only:
            stage = "new_aggregation"
            began = time.monotonic()
            result = {str(status): count for status, count in db.execute(new)}
            print(json.dumps({"business_data_read_only": True, "counts": result,
                              "seconds": round(time.monotonic() - began, 4)}, ensure_ascii=False), flush=True)
            raise SystemExit(0)
        stage = "old_aggregation"
        began = time.monotonic()
        old_counts = {str(status): count for status, count in db.execute(old)}
        old_seconds = time.monotonic() - began
        stage = "new_aggregation"
        began = time.monotonic()
        new_counts = {str(status): count for status, count in db.execute(new)}
        new_seconds = time.monotonic() - began
        if old_counts != new_counts:
            raise ValueError("新旧SKU统计不一致。")
        print(json.dumps({"read_only": True, "same_transaction": True, "shop_id": 1,
                          "same_counts": True, "counts": new_counts,
                          "old_seconds": round(old_seconds, 4), "new_seconds": round(new_seconds, 4),
                          "explain": [dict(row) for row in plan]}, ensure_ascii=False, indent=2, default=str))
except Exception as error:
    original = getattr(getattr(error, "orig", None), "args", ())
    print(json.dumps({"stage": stage, "error_type": type(error).__name__, "database_error_code": original[0] if original else None,
                      "message": "只读SKU统计对照未通过。"}, ensure_ascii=False))
    raise SystemExit(1)
finally:
    engine.dispose()
