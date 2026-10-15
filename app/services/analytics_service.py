"""Read-only, server-side analytics over collected observations.

Counts and medians use the complete filtered scope. Only scatter points and
the number of displayed groups are bounded; their coverage is explicit.
"""

from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import and_, case, cast, exists, func, literal_column, or_, select, true
from sqlalchemy.dialects.mysql import BINARY

from app.models import ChangeEvent, Product, ProductSnapshot, ProductVariant, Shop
from app.models.product_image import ProductImage
from app.services.product_images import cover_images, images_table_available
from app.services.task_runtime import database_now


GROUP_LIMIT = 100
SHOP_LIMIT = 200
SCATTER_LIMIT = 300
CENT = Decimal("0.01")


def money(value):
    return str(Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)) if value is not None else None


def percent(count, total):
    return str((Decimal(count) * 100 / total).quantize(CENT, rounding=ROUND_HALF_UP)) if total else None


def enum_value(value):
    return value.value if hasattr(value, "value") else value


def nonempty(column):
    return and_(column.is_not(None), func.trim(column) != "")


def category_expression():
    return func.coalesce(func.nullif(func.trim(Product.normalized_category), ""),
                         func.nullif(func.trim(Product.original_category), ""), "unknown")


def filters(settings, *, session=None, shop_id=None, category=None, currency=None, q=None,
            price_min=None, price_max=None, stock=None, price_upper_exclusive=False, price_unknown=None, attribute=None, value=None):
    currency = (currency or settings.currency).upper()
    scope = {"platform": settings.platform, "region": settings.region,
             "currency": currency, "pricing_context": settings.pricing_context,
             "shop_id": str(shop_id) if shop_id is not None else None,
             "category": category, "q": q.strip() if q and q.strip() else None,
             "price_min": money(price_min), "price_max": money(price_max), "stock": stock,
             "price_upper_exclusive": price_upper_exclusive, "price_unknown": price_unknown,
             "attribute": attribute, "value": value}
    clauses = [Product.platform == settings.platform, Product.region == settings.region,
               Product.currency == currency, Product.pricing_context == settings.pricing_context,
               Shop.platform == settings.platform, Shop.region == settings.region]
    if shop_id is not None:
        clauses.append(Product.shop_id == shop_id)
    if category:
        clauses.append(category_expression() == category)
    if scope["q"]:
        clauses.append(or_(Product.title.contains(scope["q"], autoescape=True),
                           Product.brand.contains(scope["q"], autoescape=True),
                           Product.platform_product_id.contains(scope["q"], autoescape=True),
                           Shop.canonical_name.contains(scope["q"], autoescape=True)))
    if price_min is not None:
        clauses.append(Product.price_min >= price_min)
    if price_max is not None:
        clauses.append(Product.price_min < price_max if price_upper_exclusive else Product.price_min <= price_max)
    if price_unknown is not None:
        clauses.append(Product.price_min.is_(None) if price_unknown else Product.price_min.is_not(None))
    if stock:
        clauses.append(Product.stock_status == stock)
    if (attribute is None) != (value is None):
        raise ValueError("属性与标签值必须同时提供。")
    if attribute:
        columns = {"brand": Product.brand, "gender": Product.gender, "season": Product.season,
                   "style": Product.style, "material": Product.material,
                   "color": Product.color_summary, "size": Product.size_summary}
        if attribute not in columns:
            raise ValueError("未知属性维度。")
        if attribute in {"color", "size"}:
            values = json_values(session, columns[attribute])
            # MySQL can decorrelate EXISTS over JSON_TABLE incorrectly. An
            # independent ID subquery also uses the exact labels of the chart.
            matching_products = select(Product.id).select_from(Product).join(values, true())\
                .where(func.trim(values.c.value) == value).correlate(None)
            clauses.append(Product.id.in_(matching_products))
        else:
            clauses.append(func.trim(columns[attribute]) == value)
    return clauses, scope


def products_query(*columns):
    return select(*columns).select_from(Product).join(Shop, Product.shop_id == Shop.id)


def sum_if(condition):
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def json_array_length(session, column):
    return func.json_length(column) if session.bind.dialect.name == "mysql" else func.json_array_length(column)


def json_values(session, column):
    if session.bind.dialect.name == "mysql":
        # JSON_TABLE is supported by the project's MySQL 8 schema target.
        return func.json_table(column, literal_column("'$[*]' COLUMNS (value VARCHAR(512) PATH '$')")).table_valued("value", joins_implicitly=True)
    return func.json_each(column).table_valued("key", "value", joins_implicitly=True)


def current_variant_condition(session):
    """Exact string membership in the latest observed SKU set.

    Ignore malformed numeric/object/nested-array entries, preserve case and
    trailing spaces, and count retained SKU rows at most once per observation.
    A scalar JSON string is also treated as a one-element observed set.
    """
    if session.bind.dialect.name == "mysql":
        values = func.json_table(Product.raw_attributes, literal_column(
            "'$.observed_sku_ids[*]' COLUMNS (raw_sku JSON PATH '$' NULL ON ERROR)"
        )).table_valued("raw_sku", joins_implicitly=True)
        match = and_(func.json_type(values.c.raw_sku) == "STRING",
            cast(func.json_unquote(values.c.raw_sku), BINARY) == cast(ProductVariant.platform_variant_id, BINARY))
    else:
        values = func.json_each(Product.raw_attributes, "$.observed_sku_ids").table_valued("key", "value", "type")
        match = and_(values.c.type == "text", values.c.value.collate("BINARY") == ProductVariant.platform_variant_id)
    root_type = func.json_type(func.json_extract(Product.raw_attributes, "$.observed_sku_ids")) if session.bind.dialect.name == "mysql" \
        else func.json_type(Product.raw_attributes, "$.observed_sku_ids")
    lookup = select(func.count()).select_from(values).where(match).correlate(Product, ProductVariant)
    # A correlated scalar aggregate also avoids MySQL's incorrect semijoin
    # decorrelation over JSON_TABLE for mixed string/numeric ID arrays.
    if session.bind.dialect.name == "mysql":
        scalar_match = cast(func.json_unquote(func.json_extract(Product.raw_attributes, "$.observed_sku_ids")), BINARY) \
            == cast(ProductVariant.platform_variant_id, BINARY)
        return or_(and_(root_type == "STRING", scalar_match), and_(root_type == "ARRAY", lookup.scalar_subquery() > 0))
    return and_(root_type.in_(["array", "text"]), exists(select(1).select_from(values).where(match)
        .correlate(Product, ProductVariant)))


def observed_variants_query(session, clauses, currency, *columns):
    """SQL membership query for bounded listings and explicit SKU sorting.

    The workbench uses the batched projection below, so it does not evaluate
    this JSON predicate repeatedly across every dashboard aggregate.
    """
    return products_query(*columns).join(ProductVariant, ProductVariant.product_id == Product.id).where(
        *clauses, ProductVariant.currency == currency, current_variant_condition(session))


@contextmanager
def workbench_variant_query(session, clauses, currency):
    """Read-only SQL reference projection retained for the profiler.

    The projection owns no temporary table and has no cleanup side effects.
    """
    yield observed_variants_query(session, clauses, currency, ProductVariant.id.label("variant_id"),
                                  Product.shop_id.label("shop_id"), ProductVariant.color,
                                  ProductVariant.size, ProductVariant.stock_status)


def shop_variant_aggregation(variant_query):
    """Build a grouped SELECT over projected fields, also usable with EXPLAIN."""
    columns = variant_query.selected_columns
    return variant_query.with_only_columns(columns.shop_id, func.count(),
        sum_if(columns.stock_status.in_(["in_stock", "out_of_stock"])),
        sum_if(columns.stock_status == "out_of_stock")).group_by(columns.shop_id)


def image_coverage(session, clauses, total, *, ready=None):
    ready = images_table_available(session) if ready is None else ready
    known = int(session.scalar(products_query(func.count(func.distinct(Product.id))).join(
        ProductImage, ProductImage.product_id == Product.id).where(*clauses,
        ProductImage.is_current.is_(True), nonempty(ProductImage.image_url))) or 0) if ready else None
    return {"field": "image", "label": "真实商品图片", "known_count": known, "total_count": total,
            "coverage_pct": percent(known, total) if known is not None else None, "ready": ready}


def aggregate_current_variants(session, clauses, currency, *, batch_size=128):
    """Read the exact current SKU set with bounded product/SKU projections.

    MySQL evaluates JSON membership for every joined SKU, and JSON_TABLE
    expansion still performs one secondary-index lookup per identifier. On
    small collection hosts both are much slower than reading a product's ID
    array once and applying exact string membership to its projected SKU rows.
    A scoped ID-only keyset cursor bounds each payload query and avoids walking
    unrelated stores or markets for a selective request. The separate JSON
    projection keeps raw payloads out of the ORDER BY/LIMIT phase, without
    forcing an index. No ORM objects or entire catalog are loaded in memory.
    """
    if not 1 <= batch_size <= 1000:
        raise ValueError("SKU统计批次必须为1-1000个商品。")
    shops, cells = defaultdict(Counter), Counter()
    upper = session.scalar(products_query(func.max(Product.id)).where(*clauses))
    if upper is None:
        return shops, cells
    after = 0
    while True:
        identifiers = session.scalars(products_query(Product.id).where(*clauses, Product.id > after, Product.id <= upper)
            .order_by(Product.id).limit(batch_size)).all()
        if not identifiers:
            break
        after = identifiers[-1]
        products = session.execute(products_query(Product.id, Product.shop_id,
            Product.raw_attributes["observed_sku_ids"]).where(*clauses, Product.id.in_(identifiers))).all()
        current = {}
        for identifier, shop, values in products:
            # JSON_CONTAINS also treats a scalar JSON string as membership.
            values = [values] if isinstance(values, str) else values if isinstance(values, list) else []
            identifiers = {value for value in values if isinstance(value, str)}
            if identifiers:
                current[identifier] = (shop, identifiers)
        if not current:
            continue
        rows = session.execute(select(ProductVariant.product_id, ProductVariant.platform_variant_id,
            ProductVariant.color, ProductVariant.size, ProductVariant.stock_status).where(
            ProductVariant.product_id.in_(current), ProductVariant.currency == currency)
            .execution_options(yield_per=1000))
        try:
            for identifier, sku, color, size, stock in rows:
                shop, identifiers = current[identifier]
                if sku not in identifiers:
                    continue
                stock = enum_value(stock)
                shops[shop]["total"] += 1
                shops[shop]["known"] += stock in {"in_stock", "out_of_stock"}
                shops[shop]["out_of_stock"] += stock == "out_of_stock"
                # SQL TRIM() removes U+0020; retain that same label semantics.
                color, size = (color or "").strip(" "), (size or "").strip(" ")
                if color and size:
                    cells[(color, size, stock)] += 1
        finally:
            rows.close()
    return shops, cells


def matrix_from_counts(cells, total):
    colors, sizes = Counter(), Counter()
    for (color, size, _), count in cells.items():
        colors[color] += count
        sizes[size] += count
    selected_colors = [key for key, _ in sorted(colors.items(), key=lambda pair: (-pair[1], pair[0]))[:12]]
    selected_sizes = [key for key, _ in sorted(sizes.items(), key=lambda pair: (-pair[1], pair[0]))[:10]]
    grouped = defaultdict(Counter)
    for (color, size, stock), count in cells.items():
        if color in selected_colors and size in selected_sizes:
            grouped[(color, size)][stock] += count
    result = [{"color": color, "size": size, "count": sum(counts.values()),
               "in_stock": counts["in_stock"], "out_of_stock": counts["out_of_stock"],
               "unknown": counts["unknown"] + counts["partial"]}
              for (color, size), counts in sorted(grouped.items())]
    eligible = sum(cells.values())
    shown = sum(row["count"] for row in result)
    return {"colors": selected_colors, "sizes": selected_sizes, "cells": result,
            "basis": "current_observed_variant", "total_variants": total, "eligible_variants": eligible,
            "shown_variants": shown, "missing_attributes_variants": total - eligible, "truncated": shown < eligible,
            "note": "只统计最近详情实际观察到且颜色和尺码均明确的 SKU；空格没有观察组合，不代表缺货。"}


def field_coverage(session, clauses, total, *, image_info=None):
    definitions = [("title", "商品标题", nonempty(Product.title)),
                   ("category", "详情类目", or_(nonempty(Product.normalized_category), nonempty(Product.original_category))),
                   ("price", "公开起价", Product.price_min.is_not(None)),
                   ("stock", "明确商品库存", Product.stock_status != "unknown"),
                   ("discount", "明确折扣", Product.discount_rate.is_not(None)),
                   ("rating", "公开评分", Product.rating.is_not(None)),
                   ("reviews", "评价数量", Product.review_count.is_not(None)),
                   ("colors", "颜色", json_array_length(session, Product.color_summary) > 0),
                   ("sizes", "尺码", json_array_length(session, Product.size_summary) > 0),
                   ("material", "材质", nonempty(Product.material)),
                   ("brand", "品牌", nonempty(Product.brand)),
                   ("style", "风格", nonempty(Product.style)),
                   ("season", "季节", nonempty(Product.season)),
                   ("gender", "适用人群", nonempty(Product.gender))]
    counts = session.execute(products_query(*(sum_if(condition) for _, _, condition in definitions)).where(*clauses)).one()
    fields = [{"field": key, "label": label, "known_count": int(count), "total_count": total,
               "coverage_pct": percent(int(count), total)} for (key, label, _), count in zip(definitions, counts)]
    fields.append(image_info if image_info is not None else image_coverage(session, clauses, total))
    return fields


def medians(session, clauses, *, partition=None, groups=None):
    """Select at most the two central prices per group; no sampling bias."""
    columns = [Product.price_min.label("price"),
               func.row_number().over(partition_by=partition, order_by=(Product.price_min, Product.id)).label("position"),
               func.count().over(partition_by=partition).label("size")]
    if partition is not None:
        columns.append(partition.label("group_key"))
    conditions = [*clauses, Product.price_min.is_not(None)]
    if groups is not None:
        conditions.append(partition.in_(groups))
    ranked = products_query(*columns).where(*conditions).subquery()
    middle = or_(ranked.c.position == func.floor((ranked.c.size + 1) / 2),
                 ranked.c.position == func.floor((ranked.c.size + 2) / 2))
    result = select(ranked.c.price)
    if partition is not None:
        result = result.add_columns(ranked.c.group_key)
    values = defaultdict(list)
    for row in session.execute(result.where(middle)):
        values[row[1] if partition is not None else "all"].append(Decimal(str(row[0])))
    return {key: money(sum(prices) / len(prices)) for key, prices in values.items()}


def attribute_distribution(session, clauses, column, total, *, array=False):
    query = products_query()
    if array:
        values = json_values(session, column)
        query = query.join(values, true())
        value = func.trim(values.c.value)
        known = int(session.scalar(products_query(func.count()).where(*clauses,
                                  json_array_length(session, column) > 0)) or 0)
    else:
        value = func.trim(column)
        known = int(session.scalar(products_query(func.count()).where(*clauses, nonempty(column))) or 0)
    grouped = query.with_only_columns(value.label("value"), func.count(func.distinct(Product.id)).label("count")).where(
        *clauses, value.is_not(None), value != "").group_by(value).order_by(func.count(func.distinct(Product.id)).desc(), value)
    rows = session.execute(grouped.limit(GROUP_LIMIT + 1)).all()
    return {"basis": "product", "known_products": known, "total_products": total,
            "coverage_pct": percent(known, total), "items": [{"value": value, "count": int(count)} for value, count in rows[:GROUP_LIMIT]],
            "truncated": len(rows) > GROUP_LIMIT, "sample_count": total,
            "note": "同一商品可含多个标签；标签保留来源文案，未做同义词合并。"}


def build_workbench(session, settings, *, lookback_hours=24, as_of=None, **options):
    """Read through the caller's transaction without changing its isolation.

    The API creates a fresh Session per cache build. REPEATABLE READ callers
    retain the same snapshot throughout product and SKU batches; explicit RC
    callers keep their existing policy. No implicit commit or flush is needed.
    """
    if not 1 <= lookback_hours <= 720:
        raise ValueError("观察窗口为 1-720 小时。")
    now = as_of or (database_now(session) if session.bind.dialect.name == "mysql" else datetime.now(timezone.utc))
    if now.tzinfo is None:
        raise ValueError("观察时间需要明确时区。")
    now = now.astimezone(timezone.utc)
    since = now - timedelta(hours=lookback_hours)
    clauses, scope = filters(settings, session=session, **options)
    with session.no_autoflush:
        stats = session.execute(products_query(func.count(), func.count(func.distinct(Product.shop_id)),
            sum_if(Product.last_success_at.is_not(None)), func.count(Product.price_min),
            sum_if(Product.stock_status != "unknown"), func.count(Product.discount_rate),
            func.min(Product.price_min), func.max(Product.price_min)).where(*clauses)).one()
        total, shops_count, detailed, price_known, stock_known, discount_known, low, high = stats
        total, detailed = int(total), int(detailed)
        overall_median = medians(session, clauses).get("all")
        sku_aggregates, matrix_counts = aggregate_current_variants(session, clauses, scope["currency"])
        shop_variant_rows = [(shop, counts["total"], counts["known"], counts["out_of_stock"])
                             for shop, counts in sku_aggregates.items()]
        # Sum all scoped shops before the separate SHOP_LIMIT presentation cap.
        variant_stats = tuple(sum(int(row[index]) for row in shop_variant_rows) for index in range(1, 4))
        matrix = matrix_from_counts(matrix_counts, int(variant_stats[0]))
        shop_skus = {row[0]: tuple(int(value) for value in row[1:]) for row in shop_variant_rows}
        category = category_expression()
        category_rows = session.execute(products_query(category, func.count(), sum_if(Product.last_success_at.is_not(None)),
            func.count(Product.price_min), func.min(Product.price_min), func.max(Product.price_min)).where(*clauses)
            .group_by(category).order_by(func.count().desc(), category).limit(GROUP_LIMIT + 1)).all()
        category_medians = medians(session, clauses, partition=category, groups=[row[0] for row in category_rows[:GROUP_LIMIT]])
        categories = [{"category": label, "count": int(count), "detail_products": int(details),
                       "price_known_count": int(known), "price_min": money(minimum), "price_max": money(maximum),
                       "price_median": category_medians.get(label), "median_sample_count": int(known)}
                      for label, count, details, known, minimum, maximum in category_rows[:GROUP_LIMIT]]
        shop_rows = session.execute(products_query(Product.shop_id, Shop.canonical_name, func.count(),
            sum_if(Product.last_success_at.is_not(None)), func.count(Product.price_min),
            func.min(Product.price_min), func.max(Product.price_min)).where(*clauses).group_by(Product.shop_id, Shop.canonical_name)
            .order_by(func.count().desc(), Product.shop_id).limit(SHOP_LIMIT + 1)).all()
        shop_medians = medians(session, clauses, partition=Product.shop_id, groups=[row[0] for row in shop_rows[:SHOP_LIMIT]])
        shops = []
        for identifier, name, count, details, known, minimum, maximum in shop_rows[:SHOP_LIMIT]:
            variant_count, known_stock, stockout = shop_skus.get(identifier, (0, 0, 0))
            shops.append({"id": str(identifier), "name": name, "products": int(count), "detail_products": int(details),
                          "variant_count": int(variant_count), "known_stock_variants": int(known_stock),
                          "out_of_stock_variants": int(stockout), "sku_stockout_pct": percent(int(stockout), int(known_stock)),
                          "price_known_count": int(known), "price_min": money(minimum), "price_max": money(maximum),
                          "price_median": shop_medians.get(identifier), "median_sample_count": int(known)})
        cutoffs = [Decimal(0), Decimal(10), Decimal(20), Decimal(30), Decimal(50), Decimal(100), None]
        histogram_conditions = [and_(Product.price_min >= lower, Product.price_min < upper) if upper is not None
                                else Product.price_min >= lower for lower, upper in zip(cutoffs, cutoffs[1:])]
        histogram_counts = session.execute(products_query(*(sum_if(condition) for condition in histogram_conditions))
                                           .where(*clauses)).one()
        histogram = [{"key": f"price_{lower}_{upper or 'plus'}", "label": f"{lower}–{upper}" if upper is not None else f"{lower}+",
                      "lower": money(lower), "upper": money(upper), "count": int(count)}
                     for lower, upper, count in zip(cutoffs, cutoffs[1:], histogram_counts)]
        histogram.append({"key": "unknown", "label": "价格未获取", "lower": None, "upper": None, "count": total - int(price_known)})
        stock_distribution = [{"status": enum_value(status), "count": int(count)} for status, count in session.execute(
            products_query(Product.stock_status, func.count()).where(*clauses).group_by(Product.stock_status))]
        discounts = [("unknown", "折扣未获取", Product.discount_rate.is_(None)),
                     ("none", "明确无折扣", Product.discount_rate == 0),
                     ("under10", "0–10%", and_(Product.discount_rate > 0, Product.discount_rate <= Decimal("0.1"))),
                     ("10to30", "10–30%", and_(Product.discount_rate > Decimal("0.1"), Product.discount_rate <= Decimal("0.3"))),
                     ("30to50", "30–50%", and_(Product.discount_rate > Decimal("0.3"), Product.discount_rate <= Decimal("0.5"))),
                     ("over50", "50%以上", Product.discount_rate > Decimal("0.5"))]
        discount_counts = session.execute(products_query(*(sum_if(condition) for _, _, condition in discounts)).where(*clauses)).one()
        discount_buckets = [{"key": key, "label": label, "count": int(count)} for (key, label, _), count in zip(discounts, discount_counts)]
        attributes = {key: attribute_distribution(session, clauses, column, total, array=array) for key, column, array in [
            ("colors", Product.color_summary, True), ("sizes", Product.size_summary, True),
            ("materials", Product.material, False), ("styles", Product.style, False),
            ("seasons", Product.season, False), ("gender", Product.gender, False), ("brands", Product.brand, False)]}
        eligible = [*clauses, Product.price_min.is_not(None), Product.rating.is_not(None)]
        scatter_count = int(session.scalar(products_query(func.count()).where(*eligible)) or 0)
        scatter_rows = session.execute(products_query(Product.id, Product.title, Product.shop_id, Product.price_min,
            Product.rating, Product.review_count).where(*eligible).order_by(Product.id).limit(SCATTER_LIMIT)).all()
        scatter = {"points": [{"id": str(identifier), "title": title, "shop_id": str(shop_id), "price_min": money(price),
                               "rating": money(rating), "review_count": reviews} for identifier, title, shop_id, price, rating, reviews in scatter_rows],
                   "eligible_count": scatter_count, "returned_count": len(scatter_rows), "truncated": scatter_count > len(scatter_rows),
                   "selection": "stable_product_id_prefix", "limit": SCATTER_LIMIT}
        freshness = [("under24h", "24 小时内", Product.last_success_at >= now - timedelta(hours=24)),
                     ("1to3d", "1–3 天", and_(Product.last_success_at < now - timedelta(hours=24), Product.last_success_at >= now - timedelta(days=3))),
                     ("3to7d", "3–7 天", and_(Product.last_success_at < now - timedelta(days=3), Product.last_success_at >= now - timedelta(days=7))),
                     ("over7d", "7 天以前", Product.last_success_at < now - timedelta(days=7)),
                     ("never", "详情待采集", Product.last_success_at.is_(None))]
        freshness_counts = session.execute(products_query(*(sum_if(condition) for _, _, condition in freshness)).where(*clauses)).one()
        image_info = image_coverage(session, clauses, total)
        quality = {"field_coverage": field_coverage(session, clauses, total, image_info=image_info),
                   "image_coverage": image_info,
                   "detail_coverage": {"known_count": detailed, "total_count": total, "coverage_pct": percent(detailed, total)},
                   "freshness_buckets": [{"key": key, "label": label, "count": int(count)} for (key, label, _), count in zip(freshness, freshness_counts)]}
        snapshots = products_query(ProductSnapshot.product_id, ProductSnapshot.captured_at).join(
            ProductSnapshot, ProductSnapshot.product_id == Product.id).where(*clauses,
            ProductSnapshot.currency == scope["currency"], ProductSnapshot.pricing_context == settings.pricing_context,
            ProductSnapshot.source_status == 200, ProductSnapshot.captured_at <= now)
        history_stats = session.execute(snapshots.with_only_columns(func.count(), func.count(func.distinct(ProductSnapshot.product_id)),
            func.min(ProductSnapshot.captured_at), func.max(ProductSnapshot.captured_at))).one()
        observation_count = func.count(func.distinct(ProductSnapshot.captured_at))
        repeat_groups = snapshots.with_only_columns(ProductSnapshot.product_id, observation_count)\
            .group_by(ProductSnapshot.product_id).having(observation_count >= 2).subquery()
        repeat_count = int(session.scalar(select(func.count()).select_from(repeat_groups)) or 0)
        earliest, latest = history_stats[2:]
        history = {"snapshot_count": int(history_stats[0]), "products_with_history": int(history_stats[1]),
                   "products_with_two_observations": repeat_count, "earliest_capture_at": earliest.isoformat() if earliest else None,
                   "latest_capture_at": latest.isoformat() if latest else None,
                   "span_hours": str(Decimal(str((latest - earliest).total_seconds() / 3600)).quantize(CENT)) if earliest else None,
                   "trend_ready": repeat_count > 0}
        events = products_query(ChangeEvent.event_type).join(ChangeEvent, ChangeEvent.product_id == Product.id).join(
            ProductSnapshot, ProductSnapshot.id == ChangeEvent.snapshot_id).where(*clauses,
            ProductSnapshot.product_id == Product.id, ProductSnapshot.currency == scope["currency"],
            ProductSnapshot.pricing_context == settings.pricing_context, ProductSnapshot.source_status == 200,
            ProductSnapshot.captured_at >= since, ProductSnapshot.captured_at <= now, ChangeEvent.detected_at <= now)
        event_types = [{"event_type": enum_value(kind), "count": int(count)} for kind, count in session.execute(
            events.with_only_columns(ChangeEvent.event_type, func.count()).group_by(ChangeEvent.event_type))]
        # The interface groups actual observations by the user's Shanghai calendar.
        if session.get_bind().dialect.name == "mysql":
            day = func.date(func.date_add(ProductSnapshot.captured_at, literal_column("INTERVAL 8 HOUR")))
        else:
            day = func.date(func.datetime(ProductSnapshot.captured_at, "+8 hours"))
        daily = defaultdict(lambda: {"count": 0, "types": {}})
        for date, kind, count in session.execute(events.with_only_columns(day, ChangeEvent.event_type, func.count()).group_by(day, ChangeEvent.event_type).order_by(day)):
            key = str(date)
            daily[key]["count"] += int(count)
            daily[key]["types"][enum_value(kind)] = int(count)
        event_trend = [{"date": date, **value} for date, value in sorted(daily.items())]
        return {"version": 1, "generated_at": now.isoformat(), "scope": scope,
                "window": {"since": since.isoformat(), "until": now.isoformat(), "lookback_hours": lookback_hours},
                "summary": {"products": total, "shops": int(shops_count), "detail_products": detailed,
                    "image_known_products": image_info["known_count"], "image_coverage_pct": image_info["coverage_pct"],
                    "variants": int(variant_stats[0]), "known_stock_variants": int(variant_stats[1]),
                    "out_of_stock_variants": int(variant_stats[2]), "sku_stockout_pct": percent(int(variant_stats[2]), int(variant_stats[1])),
                    "price_known_products": int(price_known), "stock_known_products": int(stock_known),
                    "discount_known_products": int(discount_known), "events_in_window": sum(row["count"] for row in event_types),
                    "price_stats": {"known_count": int(price_known), "min": money(low), "max": money(high), "median": overall_median,
                                    "median_basis": "full_scope", "median_sample_count": int(price_known)},
                    "detail_coverage_pct": percent(detailed, total)},
                "price_histogram": histogram, "categories": categories, "shops": shops,
                "stock_distribution": stock_distribution, "discount_buckets": discount_buckets,
                "attribute_distributions": attributes, "price_rating_scatter": scatter, "color_size_matrix": matrix,
                "collection_quality": quality, "history_summary": history,
                "event_trend_daily": event_trend, "events_by_type": event_types,
                "coverage": {"products_total": total, "aggregation": "full_scope", "median_sample_limit": None,
                             "median_sample_count": int(price_known), "median_sample_strategy": "full_scope",
                             "categories_truncated": len(category_rows) > GROUP_LIMIT, "shops_truncated": len(shop_rows) > SHOP_LIMIT,
                             "category_group_limit": GROUP_LIMIT, "shop_group_limit": SHOP_LIMIT, "event_date_timezone": "Asia/Shanghai",
                             "image_readiness": image_info["ready"],
                             "variant_basis": "latest_detail_observed_sku_ids",
                             "variant_aggregation": "batched_exact_membership", "matrix_label_comparison": "exact_source_text"},
                "notes": ["当前分布统计全部符合筛选条件的已采集商品；不代表全店或整个市场。",
                          "价格为公开标价下限，From 起价和价格区间不等于统一单价或成交价；币种与价格上下文严格隔离。",
                          "详情覆盖率以成功详情时间为依据；未知字段保留为空，未知库存和未知折扣单独展示。",
                          "SKU 仅统计最近详情明确出现的 observed_sku_ids；保留但本次未出现的 SKU 不计入当前库存。",
                          "颜色、尺码按商品去重统计，多标签总数可能超过商品数；其他属性使用采集原文，不自动推断。",
                          "颜色尺码矩阵保留来源标签的大小写和重音差异，只去除首尾普通空格，不合并同名或近似标签。",
                          "趋势来自有效历史快照与实际变化事件；首次发现时间不等于平台上架时间，新发现事件不是销量。",
                          "公开累计销量不用于日销量、收入或成交额估计；评分散点仅展示评分和价格已知的商品。",
                          "观察窗口仅限制变化事件；分布和覆盖率反映当前已采集记录，历史覆盖展示全部有效历史。"]}


def color_size_matrix(session, variant_query, total):
    """SQL reference for exact source labels, independent of server collation."""
    columns = variant_query.selected_columns
    collation = "utf8mb4_bin" if session.bind.dialect.name == "mysql" else "BINARY"
    color, size = func.trim(columns.color).collate(collation), func.trim(columns.size).collate(collation)
    query = variant_query.where(color.is_not(None), color != "", size.is_not(None), size != "")
    # The window sums all grouped colors before LIMIT selects the displayed
    # twelve; it also supplies the old separate full-scope eligibility count.
    color_rows = session.execute(query.with_only_columns(color, func.count(),
        func.sum(func.count()).over().label("eligible_total")).group_by(color)
        .order_by(func.count().desc(), color).limit(12)).all()
    eligible = int(color_rows[0][2]) if color_rows else 0
    colors = [row[0] for row in color_rows]
    sizes = [value for value, _ in session.execute(query.with_only_columns(size, func.count()).group_by(size)
             .order_by(func.count().desc(), size).limit(10))]
    cells = [{"color": c, "size": s, "count": int(count), "in_stock": int(available),
              "out_of_stock": int(stockout), "unknown": int(other)}
             for c, s, count, available, stockout, other in session.execute(query.with_only_columns(color, size, func.count(),
                 sum_if(columns.stock_status == "in_stock"), sum_if(columns.stock_status == "out_of_stock"),
                 sum_if(columns.stock_status.in_(["unknown", "partial"]))).where(color.in_(colors), size.in_(sizes))
                 .group_by(color, size).order_by(color, size))]
    shown = sum(cell["count"] for cell in cells)
    return {"colors": colors, "sizes": sizes, "cells": cells, "basis": "current_observed_variant",
            "total_variants": total, "eligible_variants": eligible, "shown_variants": shown,
            "missing_attributes_variants": total - eligible, "truncated": shown < eligible,
            "note": "只统计最近详情实际观察到且颜色和尺码均明确的 SKU；空格没有观察组合，不代表缺货。"}


def list_analytics_products(session, settings, *, page=1, page_size=24, sort="recent", **options):
    clauses, scope = filters(settings, session=session, **options)
    sort_columns = {"recent": (Product.last_seen_at.desc(),), "price_asc": (Product.price_min.asc(),),
                    "price_desc": (Product.price_min.desc(),), "title": (Product.title.asc(),), "title_desc": (Product.title.desc(),),
                    "shop_asc": (Shop.canonical_name.asc(),), "shop_desc": (Shop.canonical_name.desc(),),
                    "stock_asc": (Product.stock_status.asc(),), "stock_desc": (Product.stock_status.desc(),),
                    "sku_count_asc": (), "sku_count_desc": (),
                    "rating_desc": (Product.rating.desc(),), "reviews_desc": (Product.review_count.desc(),)}
    if not 1 <= page_size <= 100 or page < 1 or sort not in sort_columns:
        raise ValueError("无效分页或排序。")
    ordering = (*sort_columns[sort], Product.id)
    if sort.startswith("price_"):
        ordering = (Product.price_min.is_(None), *ordering)
    with session.no_autoflush:
        total = int(session.scalar(products_query(func.count()).where(*clauses)) or 0)
        query = products_query(Product, Shop.canonical_name).where(*clauses)
        if sort.startswith("sku_count_"):
            sku_counts = observed_variants_query(session, clauses, scope["currency"],
                Product.id.label("product_id"), func.count(ProductVariant.id).label("count")).group_by(Product.id).subquery()
            query = query.outerjoin(sku_counts, sku_counts.c.product_id == Product.id)
            count = func.coalesce(sku_counts.c.count, 0)
            ordering = (Product.last_success_at.is_(None), count.desc() if sort.endswith("desc") else count.asc(), Product.id)
        elif sort.startswith("stock_"):
            ordering = (Product.stock_status == "unknown", *ordering)
        rows = session.execute(query
            .order_by(*ordering).offset((page - 1) * page_size).limit(page_size)).all()
        identifiers = [product.id for product, _ in rows]
        counts = dict(session.execute(observed_variants_query(session, [*clauses, Product.id.in_(identifiers)],
            scope["currency"], Product.id, func.count(ProductVariant.id)).group_by(Product.id)).all()) if identifiers else {}
        image_ready = images_table_available(session)
        covers = cover_images(session, identifiers) if image_ready else {}
        image_info = image_coverage(session, clauses, total, ready=image_ready)
        items = []
        attributes = ("original_category", "normalized_category", "brand", "gender", "season", "style", "fit",
                      "garment_length", "material", "pattern", "sales_count", "sales_count_text", "review_count")
        for product, shop_name in rows:
            lower, upper = product.price_min, product.price_max
            item = {"id": str(product.id), "platform_product_id": product.platform_product_id,
                    "shop_id": str(product.shop_id), "shop_name": shop_name, "title": product.title,
                    "product_url": product.product_url, "category": product.normalized_category or product.original_category or "unknown",
                    "currency": product.currency, "pricing_context": product.pricing_context,
                    "price_min": money(lower), "price_max": money(upper), "original_price_min": money(product.original_price_min),
                    "original_price_max": money(product.original_price_max), "discount_rate": str(product.discount_rate) if product.discount_rate is not None else None,
                    "rating": money(product.rating), "stock_status": enum_value(product.stock_status),
                    "availability_status": enum_value(product.availability_status), "color_summary": product.color_summary or [],
                    "size_summary": product.size_summary or [], "first_seen_at": product.first_seen_at.isoformat(),
                    "last_seen_at": product.last_seen_at.isoformat(), "last_success_at": product.last_success_at.isoformat() if product.last_success_at else None,
                    "detail_collected": product.last_success_at is not None,
                    "variant_count": int(counts.get(product.id, 0)) if product.last_success_at is not None else None,
                    "price_kind": "unknown" if lower is None else "starting_price" if upper is None else "exact" if lower == upper else "range",
                    "image_url": None, "thumbnail_url": None, "image_observed_at": None}
            image = covers.get(product.id)
            if image:
                item.update(image)
                item["image_observed_at"] = image["image_observed_at"].isoformat()
            item.update({name: getattr(product, name) for name in attributes})
            items.append(item)
        return {"items": items, "total": total, "page": page, "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size, "scope": scope,
                "field_coverage": field_coverage(session, clauses, total, image_info=image_info), "image_readiness": image_ready}
