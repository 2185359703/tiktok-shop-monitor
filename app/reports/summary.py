"""Read bounded, scoped observations; never turn discovery timestamps into sales."""

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select
from sqlalchemy.orm import load_only

from app.models import ChangeEvent, Product, ProductSnapshot, ProductVariant, Shop, StockStatus, VariantSnapshot
from app.services.snapshot_service import normalize_json
from app.services.task_runtime import database_now


REPORT_VERSION = 1
MAX_PRODUCTS = 100000
MAX_EVENTS = 1000
PRODUCT_BATCH_SIZE = 250


def price_statistics(values):
    numbers = sorted(Decimal(str(value)) for value in values if value is not None)
    if not numbers:
        return {"known_count": 0, "min": None, "max": None, "median": None}
    middle = len(numbers) // 2
    median = numbers[middle] if len(numbers) % 2 else (numbers[middle - 1] + numbers[middle]) / 2
    return {"known_count": len(numbers), "min": str(numbers[0]), "max": str(numbers[-1]),
            "median": str(median.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))}


def percentage(numerator, denominator):
    if not denominator: return None
    return str((Decimal(numerator) * 100 / denominator).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def product_records(session, settings, as_of, by_shop):
    """Page narrow rows and aggregate SKU states in SQL, not Python ORM lists.

    A full batch can have hundreds of thousands of SKU snapshots. Loading all
    those objects or every snapshot's raw JSON exceeds a small worker's memory.
    Keep the report's public product records, but bound intermediate data to one
    product page and read only the historical fields the report actually uses.
    """
    scope = (Product.platform == settings.platform, Product.region == settings.region)
    total = session.scalar(select(func.count()).select_from(Product).where(*scope, Product.first_seen_at <= as_of))
    if total > MAX_PRODUCTS:
        raise ValueError(f"报表范围超过 {MAX_PRODUCTS} 个商品，需要分范围导出；不会截断冒充全量。")
    records, excluded_context, historical_count, cursor = [], 0, 0, 0
    while True:
        products = session.scalars(select(Product).where(*scope, Product.first_seen_at <= as_of, Product.id > cursor)
            .order_by(Product.id).limit(PRODUCT_BATCH_SIZE).options(load_only(
                Product.id, Product.shop_id, Product.platform_product_id, Product.product_url, Product.title,
                Product.original_category, Product.currency, Product.pricing_context, Product.last_seen_at,
                Product.price_min, Product.price_max, Product.discount_rate, Product.stock_status))).all()
        if not products:
            break
        cursor, identifiers = products[-1].id, [product.id for product in products]
        ranked = select(ProductSnapshot.id, func.row_number().over(partition_by=ProductSnapshot.product_id,
                        order_by=(ProductSnapshot.captured_at.desc(), ProductSnapshot.id.desc())).label("position")).where(
                        ProductSnapshot.product_id.in_(identifiers), ProductSnapshot.captured_at <= as_of,
                        ProductSnapshot.source_status == 200, ProductSnapshot.pricing_context == settings.pricing_context,
                        ProductSnapshot.currency == settings.currency).subquery()
        observation = ProductSnapshot.raw_payload["observation"]
        rows = session.execute(select(ProductSnapshot, func.json_type(observation), func.json_length(observation),
                observation["title"].as_string(), observation["original_category"].as_string(),
                func.json_length(observation["raw_attributes"]["attribute_conflicts"]))
            .where(ProductSnapshot.id.in_(select(ranked.c.id).where(ranked.c.position == 1)))
            .options(load_only(ProductSnapshot.id, ProductSnapshot.product_id, ProductSnapshot.captured_at,
                               ProductSnapshot.price_min, ProductSnapshot.price_max, ProductSnapshot.discount_rate,
                               ProductSnapshot.stock_status))).all()
        latest = {row[0].product_id: row for row in rows}
        historical_count += len(latest)
        sku_states_by_snapshot = defaultdict(Counter)
        snapshot_ids = [row[0].id for row in rows]
        for snapshot_id, status, count in session.execute(select(VariantSnapshot.product_snapshot_id,
                VariantSnapshot.stock_status, func.count()).where(VariantSnapshot.product_snapshot_id.in_(snapshot_ids))
                .group_by(VariantSnapshot.product_snapshot_id, VariantSnapshot.stock_status)):
            sku_states_by_snapshot[snapshot_id][status.value] = count
        for product in products:
            row = latest.get(product.id)
            snapshot = row[0] if row else None
            has_observation = row is not None and row[1] == "OBJECT" and (row[2] or 0) > 0
            current_visible = product.last_seen_at <= as_of
            if not snapshot and current_visible and (product.currency != settings.currency or product.pricing_context != settings.pricing_context):
                excluded_context += 1
                continue
            def observed(field):
                return getattr(snapshot, field) if snapshot is not None else getattr(product, field) if current_visible else None
            shop = by_shop[product.shop_id]
            title = row[3] if has_observation else product.title if current_visible else None
            category = row[4] if has_observation else product.original_category if current_visible else None
            price_min, price_max = observed("price_min"), observed("price_max")
            price_kind = "unknown" if price_min is None else "starting_price" if price_max is None else "exact" if Decimal(str(price_min)) == Decimal(str(price_max)) else "range"
            sku_states = sku_states_by_snapshot[snapshot.id] if snapshot else Counter()
            records.append({"id": str(product.id), "platform_product_id": product.platform_product_id,
                        "shop_id": str(shop.id), "shop_name": shop.canonical_name, "platform_shop_id": shop.platform_shop_id,
                        "title": title, "product_url": product.product_url,
                        "category": category or "unknown", "category_basis": "platform_category" if category else "unknown",
                        "currency": settings.currency, "pricing_context": settings.pricing_context,
                        "price_min": str(price_min) if price_min is not None else None,
                        "price_max": str(price_max) if price_max is not None else None, "price_kind": price_kind,
                        "discount_rate": str(observed("discount_rate")) if observed("discount_rate") is not None else None,
                        "stock_status": str(observed("stock_status") or StockStatus.UNKNOWN),
                        "observed_at": snapshot.captured_at.isoformat() if snapshot else product.last_seen_at.isoformat() if current_visible else None,
                        "snapshot_id": str(snapshot.id) if snapshot else None,
                        "sku_count": sum(sku_states.values()),
                        "sku_out_of_stock": sku_states["out_of_stock"], "sku_in_stock": sku_states["in_stock"],
                        "sku_unknown": sku_states["unknown"] + sku_states["partial"],
                        "attribute_conflict_count": (row[5] or 0) if has_observation else 0,
                        "evidence_level": "historical_detail" if snapshot else "current_observation" if current_visible else "identity_only_at_cutoff"})
    return records, historical_count, excluded_context


def build_monitoring_report(session, settings, *, as_of=None, lookback_hours=24):
    now = database_now(session)
    as_of = as_of or now
    if as_of.tzinfo is None or as_of > now or not 1 <= lookback_hours <= 720:
        raise ValueError("报表截止时间必须带时区且不在未来，观察区间为 1-720 小时。")
    as_of = as_of.astimezone(timezone.utc)
    scope = (Product.platform == settings.platform, Product.region == settings.region)
    shops = session.scalars(select(Shop).where(Shop.platform == settings.platform, Shop.region == settings.region)
                           .order_by(Shop.id)).all()
    by_shop = {shop.id: shop for shop in shops}
    records, historical_count, excluded_context = product_records(session, settings, as_of, by_shop)
    grouped = defaultdict(list)
    for record in records: grouped[record["shop_id"]].append(record)
    shop_summaries = []
    for shop in shops:
        sample = grouped[str(shop.id)]
        if not shop.canonical_url and not sample: continue
        known = sum(row["sku_in_stock"] + row["sku_out_of_stock"] for row in sample)
        stockout = sum(row["sku_out_of_stock"] for row in sample)
        shop_summaries.append({"id": str(shop.id), "platform_shop_id": shop.platform_shop_id, "name": shop.canonical_name,
                               "source_group": shop.discovery_category or "unknown", "shop_url": shop.canonical_url,
                               "sampled_products": len(sample), "historical_detail_products": sum(row["snapshot_id"] is not None for row in sample),
                               "sku_count": sum(row["sku_count"] for row in sample), "sku_out_of_stock": stockout,
                               "known_stock_skus": known, "sku_stockout_pct": percentage(stockout, known),
                               "starting_price_stats": price_statistics(row["price_min"] for row in sample)})
    shop_summaries.sort(key=lambda row: (-row["sampled_products"], row["name"], row["platform_shop_id"]))
    categories = []
    for name in sorted({row["category"] for row in records}):
        group = [row for row in records if row["category"] == name]
        categories.append({"category": name, "products": len(group), "starting_price_stats": price_statistics(row["price_min"] for row in group)})
    cutoffs = [Decimal(0), Decimal(20), Decimal(40), Decimal(60), Decimal(80), Decimal(100), None]
    histogram = []
    for lower, upper in zip(cutoffs, cutoffs[1:]):
        count = sum(row["price_min"] is not None and Decimal(row["price_min"]) >= lower
                    and (upper is None or Decimal(row["price_min"]) < upper) for row in records)
        histogram.append({"label": f"${lower}–{upper}" if upper is not None else f"${lower}+", "count": count})
    since = as_of - timedelta(hours=lookback_hours)
    event_options = func.json_extract(ProductSnapshot.raw_payload, func.concat(
        '$.observed_content.variant_options."', ProductVariant.platform_variant_id, '"'))
    event_query = select(ChangeEvent, ProductSnapshot.captured_at,
                         ProductSnapshot.raw_payload["observation"]["title"].as_string(),
                         ProductVariant.platform_variant_id, event_options).select_from(ChangeEvent)\
        .join(Product, Product.id == ChangeEvent.product_id).join(
                  ProductSnapshot, ProductSnapshot.id == ChangeEvent.snapshot_id).outerjoin(
                  ProductVariant, ProductVariant.id == ChangeEvent.variant_id).where(*scope,
                  ProductSnapshot.currency == settings.currency, ProductSnapshot.pricing_context == settings.pricing_context,
                  ProductSnapshot.captured_at >= since, ProductSnapshot.captured_at <= as_of,
                  ChangeEvent.detected_at <= now).order_by(ChangeEvent.id.desc())
    total_events = session.scalar(select(func.count()).select_from(event_query.with_only_columns(ChangeEvent.id).subquery()))
    event_rows = session.execute(event_query.limit(MAX_EVENTS)).all()
    event_product_ids = {row[0].product_id for row in event_rows}
    products_by_id = {product.id: product for product in session.scalars(select(Product).where(
        Product.id.in_(event_product_ids)).options(load_only(Product.id, Product.shop_id, Product.platform_product_id)))}
    events = []
    for event, captured_at, title, platform_variant_id, options in event_rows:
        product = products_by_id.get(event.product_id)
        if product is None: continue
        options = json.loads(options) if isinstance(options, str) else options
        if not isinstance(options, dict): options = {}
        events.append({"id": str(event.id), "product_id": str(product.id), "platform_product_id": product.platform_product_id,
                       "shop_name": by_shop[product.shop_id].canonical_name, "title": title,
                       "event_type": event.event_type.value, "variant_id": str(event.variant_id) if event.variant_id is not None else None,
                       "platform_variant_id": platform_variant_id, "variant_options": options,
                       "snapshot_id": str(event.snapshot_id), "previous_snapshot_id": str(event.previous_snapshot_id) if event.previous_snapshot_id is not None else None,
                       "old_value": event.old_value, "new_value": event.new_value, "observed_at": captured_at.isoformat(),
                       "detected_at": event.detected_at.isoformat()})
    known_stock = sum(row["sku_in_stock"] + row["sku_out_of_stock"] for row in records)
    stockout = sum(row["sku_out_of_stock"] for row in records)
    summary = {"shop_entries": sum(shop.canonical_url is not None for shop in shops),
               "sampled_shops": sum(row["sampled_products"] > 0 for row in shop_summaries), "products": len(records),
               "historical_detail_products": historical_count, "observed_skus": sum(row["sku_count"] for row in records),
               "known_stock_skus": known_stock, "stockout_skus": stockout, "sku_stockout_pct": percentage(stockout, known_stock),
               "events_in_window": total_events, "excluded_price_context_products": excluded_context,
               "price_kinds": dict(Counter(row["price_kind"] for row in records)),
               "starting_price_stats": price_statistics(row["price_min"] for row in records)}
    return normalize_json({"report_version": REPORT_VERSION, "platform": settings.platform, "region": settings.region,
                          "currency": settings.currency, "pricing_context": settings.pricing_context,
                          "as_of": as_of.isoformat(), "since": since.isoformat(), "generated_at": now.isoformat(),
                          "summary": summary, "shops": shop_summaries, "categories": categories, "price_histogram": histogram,
                          "products": records, "events": events, "events_truncated": total_events > len(events),
                          "notes": ["仅统计已采集样本，不代表全店或美国服装市场。店铺入口数是当前库内有公开链接的店铺数。",
                                    "价格分布使用每件商品的公开价格下限；From 起价不等于统一单价，不是成交价。",
                                    "SKU 缺货率仅以本次有效历史观察中状态明确的 SKU 为分母，缺失/未知不当作缺货。",
                                    "店铺来源分组不等于主营类目；没有公开详情类目的商品归入 unknown。",
                                    "销量未用于市场排名、日销量或成交额估算；本系统发现时间不代表市场上架日期。",
                                    "截止时间以前没有可重建观察的商品只显示已知身份，不能把后来数据伪装成历史。"]})
