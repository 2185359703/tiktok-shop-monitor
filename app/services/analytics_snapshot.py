"""Background aggregation over projections and nonblocking snapshot reads."""

import json
import time
from copy import deepcopy
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import Integer, JSON, case, func, literal_column, select, true, type_coerce

from app.models import Product, Shop
from app.models.analytics_read import AnalyticsRefreshRequest as Job, AnalyticsSnapshot as Snapshot, EventReadModel as E, ProductReadModel as P
from app.reports.summary import percentage, price_statistics
from app.services.analytics_queries import event_query, product_query
from app.services.analytics_projection import json_value
from app.services.analytics_refresh import VERSION, STAT_KINDS, enqueue, job_row, key_for, now, scope
from app.services.analytics_service import GROUP_LIMIT, SCATTER_LIMIT, SHOP_LIMIT, matrix_from_counts, percent


FIELD_LABELS = {"title": "商品标题", "category": "详情类目", "price": "公开起价", "stock": "明确商品库存",
    "discount": "明确折扣", "rating": "公开评分", "reviews": "评价数量", "colors": "颜色", "sizes": "尺码",
    "material": "材质", "brand": "品牌", "style": "风格", "season": "季节", "gender": "适用人群"}
ATTRIBUTES = {"colors": "color_summary", "sizes": "size_summary", "materials": "material", "styles": "style",
              "seasons": "season", "gender": "gender", "brands": "brand"}


class Busy(RuntimeError):
    pass


def ttl(kind):
    return 15 if kind == "task_counts" else 30 if kind == "events_summary" else 300


def read_snapshot(db, kind, params):
    """Never run a builder in an HTTP request, even after restart or expiry."""
    stamp, key = now(db), key_for(kind, params)
    snapshot = db.get(Snapshot, (VERSION, key))
    row = db.get(Job, (VERSION, kind, key))
    if snapshot is None or stamp >= snapshot.fresh_until:
        if row is None:
            # Serialize admission across API processes; existing keys don't consume another slot.
            job_row(db, "control", {**{k: params[k] for k in ("platform", "region", "currency", "pricing_context")}, "name": "admission"})
            count = db.scalar(select(func.count()).select_from(Job).where(Job.schema_version == VERSION,
                Job.kind.in_(STAT_KINDS), Job.requested_revision > Job.completed_revision))
            keys = db.scalar(select(func.count()).select_from(Job).where(Job.schema_version == VERSION, Job.kind.in_(STAT_KINDS)))
            if count >= 32 or keys >= 128:
                raise Busy("analytics_busy")
        if row is None or row.requested_revision == row.completed_revision:
            row = enqueue(db, kind, params)
    if snapshot is not None and stamp < snapshot.stale_until:
        return 200, {**snapshot.payload, "_meta": {"schema_version": VERSION, "generation": str(snapshot.generation),
            "status": "fresh" if stamp < snapshot.fresh_until else "stale", "snapshot_at": snapshot.snapshot_at.isoformat(),
            **snapshot.payload.get("_source", {}),
            "generated_at": snapshot.generated_at.isoformat(), "refresh_state": "failed" if row and row.last_error_code else
            "queued" if row and row.requested_revision > row.completed_revision else "idle"}}
    return 202, {"_meta": {"schema_version": VERSION, "status": "pending", "query_key": key,
                           "refresh_state": "failed" if row and row.last_error_code else "queued"}}


def publish_snapshot(db, kind, params, revision, payload, snapshot_at):
    size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    if size > 512 * 1024:
        raise ValueError("analytics_snapshot_too_large")
    stamp, key = now(db), key_for(kind, params)
    row = db.get(Snapshot, (VERSION, key))
    if row and row.generation > revision:
        return
    values = dict(schema_version=VERSION, key_hash=key, kind=kind, parameters=params, payload=payload,
        generation=revision, snapshot_at=snapshot_at, generated_at=stamp,
        fresh_until=stamp + timedelta(seconds=ttl(kind)),
        stale_until=stamp + timedelta(seconds=60 if kind == "task_counts" else 900))
    if row is None:
        db.add(Snapshot(**values))
    else:
        for name, value in values.items():
            setattr(row, name, value)


def rows_for(db, params, basis, deadline, *, include_facts=True):
    after = 0
    batch_size = 512
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("analytics_build_budget")
        payload = P.payload
        if basis == "current":
            # Image URLs dominate list payload size but statistics only need
            # presence. Keep the actual URLs untouched in the paging table.
            payload = type_coerce(func.json_remove(func.json_set(P.payload, "$.image_url",
                case((func.length(P.payload["image_url"].as_string()) > 0, 1), else_=0)),
                "$.thumbnail_url", "$.product_url", "$.image_observed_at", "$.first_seen_at", "$.last_seen_at",
                "$.original_price_min", "$.original_price_max", "$.price_max", "$.availability_status",
                "$.currency", "$.pricing_context", "$.platform_product_id", "$.fit", "$.garment_length", "$.pattern",
                "$.sales_count", "$.sales_count_text", "$.price_kind"), JSON).label("payload")
        columns = [P.product_id, P.shop_id, P.category, P.price_min, payload, Shop.canonical_name]
        if include_facts:
            columns.append(P.facts)
        query = product_query(db, params, basis).with_only_columns(*columns).where(P.product_id > after).with_hint(
            P, "FORCE INDEX (PRIMARY)", dialect_name="mysql")
        rows = db.execute(query.order_by(P.product_id).limit(batch_size)).all()
        for row in rows:
            yield row, {**row.payload, "shop_name": row.canonical_name}
        if len(rows) < batch_size:
            return
        after = rows[-1].product_id


def coverage_flags(data):
    has = lambda key: bool(str(data.get(key) or "").strip())
    return {"title": has("title"), "category": has("normalized_category") or has("original_category"),
        "price": data.get("price_min") is not None, "stock": data.get("stock_status") != "unknown",
        "discount": data.get("discount_rate") is not None, "rating": data.get("rating") is not None,
        "reviews": data.get("review_count") is not None, "colors": bool(data.get("color_summary")),
        "sizes": bool(data.get("size_summary")), **{key: has(key) for key in ("material", "brand", "style", "season", "gender")}}


def event_stats(db, params, stamp, *, product_filtered=False):
    hours = int(params.get("lookback_hours", 24))
    end = datetime.fromisoformat(params["until"]) if params.get("until") else stamp
    start = datetime.fromisoformat(params["since"]) if params.get("since") else end - timedelta(hours=hours)
    query = event_query({k: v for k, v in params.items() if not product_filtered or k not in {"q", "shop_id"}}).where(
        E.observed_at >= start, E.observed_at <= end)
    if params.get("feed_upper_sequence") is not None:
        query = query.where(E.published_sequence <= int(params["feed_upper_sequence"]))
    if product_filtered:
        matching = product_query(db, params).with_only_columns(P.product_id)
        query = query.where(E.product_id.in_(matching))
    # Group in SQL; do not load all events just to draw a bounded timeline.
    day = func.date(func.datetime(E.observed_at, "+8 hours")) if db.bind.dialect.name == "sqlite" else func.date(
        func.timestampadd(literal_column("HOUR"), 8, E.observed_at))
    buckets = db.execute(query.with_only_columns(day, E.event_type, func.count()).group_by(day, E.event_type)).all()
    types, daily = Counter(), defaultdict(lambda: {"count": 0, "types": {}})
    for date, kind, count in buckets:
        types[kind] += count
        daily[str(date)]["count"] += count
        daily[str(date)]["types"][kind] = count
    # Overview uses hourly buckets up to 48h, otherwise Shanghai calendar days.
    period = 86400 if hours > 48 else 3600
    epoch = func.strftime("%s", E.observed_at).cast(Integer) if db.bind.dialect.name == "sqlite" else func.unix_timestamp(E.observed_at)
    bucket = func.floor((epoch + 28800) / period) * period - 28800
    trend = [{"time": int(t) * 1000, "count": int(n)} for t, n in db.execute(query.with_only_columns(bucket, func.count()).group_by(bucket).order_by(bucket))]
    recent = db.execute(query.order_by(E.observed_at.desc(), E.event_id.desc()).limit(5)).all()
    return {"window": {"since": start.isoformat(), "until": end.isoformat(), "lookback_hours": hours},
        "events_in_window": sum(types.values()), "events_by_type": [{"event_type": k, "count": v} for k, v in sorted(types.items())],
        "event_trend_daily": [{"date": k, **v} for k, v in sorted(daily.items())], "event_buckets": trend,
        "events": [{**r.payload, "shop_name": name} for r, name in recent]}


def build_current(db, params, stamp, deadline):
    total = detailed = image_count = 0
    prices, scatter = [], []
    groups, categories = {}, {}
    flags, stocks, discounts, freshness, attributes, known_attributes = Counter(), Counter(), Counter(), Counter(), {}, Counter()
    for key in ATTRIBUTES:
        attributes[key] = Counter()
    sku_states, cells = Counter(), Counter()
    history_count = history_products = repeated = eligible_scatter = 0
    first_history = last_history = None
    for row, data in rows_for(db, params, "current", deadline):
        total += 1
        price = Decimal(data["price_min"]) if data.get("price_min") is not None else None
        if price is not None:
            prices.append(price)
        detailed += int(data["detail_collected"])
        image_count += int(bool(data.get("image_url")))
        flags.update({k: int(v) for k, v in coverage_flags(data).items()})
        stocks[data["stock_status"]] += 1
        discount = Decimal(data["discount_rate"]) if data.get("discount_rate") is not None else None
        discounts["unknown" if discount is None else "none" if discount == 0 else "under10" if discount <= Decimal(".1") else "10to30" if discount <= Decimal(".3") else "30to50" if discount <= Decimal(".5") else "over50"] += 1
        seen = datetime.fromisoformat(data["last_success_at"]) if data.get("last_success_at") else None
        age = (stamp - seen).total_seconds() if seen else None
        freshness["never" if age is None else "under24h" if age <= 86400 else "1to3d" if age <= 259200 else "3to7d" if age <= 604800 else "over7d"] += 1
        states = Counter(row.facts.get("sku_states", {}))
        sku_states.update(states)
        for color, size, status, count in row.facts.get("sku_cells", []):
            cells[(color, size, status)] += count
        for mapping, key in ((groups, row.shop_id), (categories, row.category)):
            group = mapping.setdefault(key, {"prices": [], "count": 0, "detail": 0, "skus": Counter(), "name": data["shop_name"]})
            group["count"] += 1
            group["detail"] += int(data["detail_collected"])
            group["skus"].update(states)
            if price is not None:
                group["prices"].append(price)
        for key, field in ATTRIBUTES.items():
            value = data.get(field)
            known_attributes[key] += int(bool(value) if isinstance(value, list) else bool(str(value or "").strip()))
            values = value if isinstance(value, list) else [value]
            attributes[key].update({str(v).strip() for v in values if v is not None and str(v).strip()})
        if price is not None and data.get("rating") is not None:
            eligible_scatter += 1
            if len(scatter) < SCATTER_LIMIT:
                scatter.append({k: data.get(k) for k in ("id", "title", "shop_id", "price_min", "rating", "review_count")})
        h = row.facts
        history_count += h.get("history_count", 0)
        history_products += int(h.get("history_count", 0) > 0)
        repeated += int(h.get("history_observations", 0) >= 2)
        if h.get("history_first"):
            first_history = min(first_history or h["history_first"], h["history_first"])
            last_history = max(last_history or h["history_last"], h["history_last"])
    price_stats = price_statistics(prices)
    price_stats.update(median_basis="full_scope", median_sample_count=len(prices))
    field_coverage = [{"field": k, "label": label, "known_count": flags[k], "total_count": total,
                       "coverage_pct": percent(flags[k], total)} for k, label in FIELD_LABELS.items()]
    image_info = {"field": "image", "label": "真实商品图片", "known_count": image_count, "total_count": total,
                  "coverage_pct": percent(image_count, total), "ready": True}
    field_coverage.append(image_info)
    cats, shops = [], []
    for key, group in sorted(categories.items(), key=lambda pair: (-pair[1]["count"], pair[0]))[:GROUP_LIMIT]:
        stats = price_statistics(group["prices"])
        cats.append(dict(category=key, count=group["count"], detail_products=group["detail"], price_known_count=len(group["prices"]),
            price_min=stats["min"], price_max=stats["max"], price_median=stats["median"], median_sample_count=len(group["prices"])))
    for key, group in sorted(groups.items(), key=lambda pair: (-pair[1]["count"], pair[0]))[:SHOP_LIMIT]:
        stats, states = price_statistics(group["prices"]), group["skus"]
        known = states["in_stock"] + states["out_of_stock"]
        shops.append(dict(id=str(key), name=group["name"], products=group["count"], detail_products=group["detail"],
            variant_count=sum(states.values()), known_stock_variants=known, out_of_stock_variants=states["out_of_stock"],
            sku_stockout_pct=percent(states["out_of_stock"], known), price_known_count=len(group["prices"]),
            price_min=stats["min"], price_max=stats["max"], price_median=stats["median"], median_sample_count=len(group["prices"])))
    histogram = []
    boundaries = [0, 10, 20, 30, 50, 100, None]
    for lower, upper in zip(boundaries, boundaries[1:]):
        histogram.append({"key": f"price_{lower}_{upper or 'plus'}", "label": f"{lower}–{upper}" if upper else f"{lower}+",
            "lower": f"{lower:.2f}", "upper": f"{upper:.2f}" if upper else None,
            "count": sum(p >= lower and (upper is None or p < upper) for p in prices)})
    histogram.append(dict(key="unknown", label="价格未获取", lower=None, upper=None, count=total - len(prices)))
    distributions = {key: {"basis": "product", "known_products": known_attributes[key], "total_products": total,
        "coverage_pct": percent(known_attributes[key], total), "items": [{"value": k, "count": n} for k, n in sorted(
            counter.items(), key=lambda pair: (-pair[1], pair[0]))[:GROUP_LIMIT]], "truncated": len(counter) > GROUP_LIMIT,
        "sample_count": total, "note": "同一商品可含多个标签；标签保留来源文案，未做同义词合并。"} for key, counter in attributes.items()}
    events = event_stats(db, params, stamp, product_filtered=True)
    known_stock = sku_states["in_stock"] + sku_states["out_of_stock"]
    summary = dict(products=total, shops=len(groups), detail_products=detailed, image_known_products=image_count,
        image_coverage_pct=percent(image_count, total), variants=sum(sku_states.values()), known_stock_variants=known_stock,
        out_of_stock_variants=sku_states["out_of_stock"], sku_stockout_pct=percent(sku_states["out_of_stock"], known_stock),
        price_known_products=len(prices), stock_known_products=flags["stock"], discount_known_products=flags["discount"],
        events_in_window=events["events_in_window"], price_stats=price_stats, detail_coverage_pct=percent(detailed, total))
    history = dict(snapshot_count=history_count, products_with_history=history_products, products_with_two_observations=repeated,
        earliest_capture_at=first_history, latest_capture_at=last_history, trend_ready=repeated > 0,
        span_hours=str(Decimal(str((datetime.fromisoformat(last_history) - datetime.fromisoformat(first_history)).total_seconds() / 3600)).quantize(Decimal(".01"))) if first_history else None)
    return {"version": 2, "generated_at": stamp.isoformat(), "scope": params, "summary": summary,
        "total": total, "field_coverage": field_coverage, "categories": cats, "shops": shops, "price_histogram": histogram,
        "stock_distribution": [{"status": k, "count": n} for k, n in sorted(stocks.items())],
        "discount_buckets": [{"key": k, "label": label, "count": discounts[k]} for k, label in (
            ("unknown", "折扣未获取"), ("none", "明确无折扣"), ("under10", "0–10%"), ("10to30", "10–30%"), ("30to50", "30–50%"), ("over50", "50%以上"))],
        "attribute_distributions": distributions, "price_rating_scatter": {"points": scatter, "eligible_count": eligible_scatter,
            "returned_count": len(scatter), "truncated": eligible_scatter > len(scatter), "selection": "stable_product_id_prefix", "limit": SCATTER_LIMIT},
        "color_size_matrix": matrix_from_counts(cells, sum(sku_states.values())), "history_summary": history,
        "collection_quality": {"field_coverage": field_coverage, "image_coverage": image_info,
            "detail_coverage": {"known_count": detailed, "total_count": total, "coverage_pct": percent(detailed, total)},
            "freshness_buckets": [{"key": k, "label": label, "count": freshness[k]} for k, label in (
                ("under24h", "24 小时内"), ("1to3d", "1–3 天"), ("3to7d", "3–7 天"), ("over7d", "7 天以前"), ("never", "详情待采集"))]},
        "coverage": {"products_total": total, "aggregation": "projected_full_scope", "median_sample_limit": None,
            "median_sample_count": len(prices), "median_sample_strategy": "full_scope", "categories_truncated": len(categories) > GROUP_LIMIT,
            "shops_truncated": len(groups) > SHOP_LIMIT, "category_group_limit": GROUP_LIMIT, "shop_group_limit": SHOP_LIMIT,
            "event_date_timezone": "Asia/Shanghai", "image_readiness": True, "variant_basis": "latest_detail_observed_sku_ids"},
        **{k: events[k] for k in ("window", "events_by_type", "event_trend_daily")},
        "notes": ["统计完整筛选范围内已采集观察，不代表全市场。", "当前分布与实际历史事件分开统计；未知值保持为空。"]}


def build_overview(db, params, stamp, deadline):
    shops = db.scalars(select(Shop).where(Shop.platform == params["platform"], Shop.region == params["region"])).all()
    summaries = {s.id: dict(id=str(s.id), platform_shop_id=s.platform_shop_id, name=s.canonical_name,
        source_group=s.discovery_category or "unknown", shop_url=s.canonical_url, sampled_products=0,
        historical_detail_products=0, sku_count=0, sku_out_of_stock=0, known_stock_skus=0) for s in shops}
    prices, shop_prices, category_prices, category_counts = [], defaultdict(list), defaultdict(list), Counter()
    totals, kinds, stocks = Counter(), Counter(), Counter()
    for row, data in rows_for(db, params, "latest_valid_snapshot", deadline, include_facts=False):
        totals["products"] += 1
        totals["historical_detail_products"] += int(data["snapshot_id"] is not None)
        target = summaries[row.shop_id]
        target["sampled_products"] += 1
        target["historical_detail_products"] += int(data["snapshot_id"] is not None)
        for name in ("sku_count", "sku_out_of_stock"):
            target[name] += data[name]
        target["known_stock_skus"] += data["sku_in_stock"] + data["sku_out_of_stock"]
        stocks.update({key: data[key] for key in ("sku_in_stock", "sku_out_of_stock", "sku_unknown")})
        kinds[data["price_kind"]] += 1
        category_counts[data["category"]] += 1
        if data["price_min"] is not None:
            price = Decimal(data["price_min"])
            prices.append(price)
            shop_prices[row.shop_id].append(price)
            category_prices[data["category"]].append(price)
    result_shops = []
    for s in shops:
        item = summaries[s.id]
        if item["shop_url"] or item["sampled_products"]:
            item.update(starting_price_stats=price_statistics(shop_prices[s.id]),
                        sku_stockout_pct=percentage(item["sku_out_of_stock"], item["known_stock_skus"]))
            result_shops.append(item)
    result_shops.sort(key=lambda row: (-row["sampled_products"], row["name"], row["platform_shop_id"]))
    events = event_stats(db, params, stamp)
    known = stocks["sku_in_stock"] + stocks["sku_out_of_stock"]
    summary = dict(totals, shop_entries=sum(s.canonical_url is not None for s in shops),
        sampled_shops=sum(s["sampled_products"] > 0 for s in result_shops), observed_skus=sum(stocks.values()),
        known_stock_skus=known, stockout_skus=stocks["sku_out_of_stock"], sku_stockout_pct=percentage(stocks["sku_out_of_stock"], known),
        price_kinds=dict(kinds), starting_price_stats=price_statistics(prices), events_in_window=events["events_in_window"])
    summary.setdefault("products", 0)
    summary.setdefault("historical_detail_products", 0)
    summary["excluded_price_context_products"] = db.scalar(select(func.count()).select_from(P).where(
        P.schema_version == VERSION, P.basis == "latest_valid_snapshot", P.platform == params["platform"],
        P.region == params["region"], P.eligible.is_(False)))
    catalog_count = db.scalar(select(func.count()).select_from(P).where(P.schema_version == VERSION, P.basis == "current",
        P.platform == params["platform"], P.region == params["region"]))
    histogram = [{"label": f"${low}–{high}" if high else f"${low}+", "count": sum(p >= low and (high is None or p < high) for p in prices)}
                 for low, high in zip([0, 20, 40, 60, 80, 100], [20, 40, 60, 80, 100, None])]
    return {"report_version": 2, **{k: params[k] for k in ("platform", "region", "currency", "pricing_context")}, "generated_at": stamp.isoformat(), "as_of": stamp.isoformat(),
        "since": events["window"]["since"], "summary": summary, "catalog_product_count": catalog_count,
        "products": [], "shops": result_shops, "categories": [{"category": k, "products": n,
            "starting_price_stats": price_statistics(category_prices[k])} for k, n in sorted(category_counts.items())],
        "price_histogram": histogram, "sku_totals": dict(stocks), "events": events["events"],
        "events_truncated": events["events_in_window"] > len(events["events"]), "event_buckets": events["event_buckets"],
        "notes": ["仅统计已采集样本。", "价格为公开起价；未知库存不计作缺货。", "完整历史报告按实际截止时间独立生成。"]}


def build_catalog_stats(db, params, stamp, deadline):
    total = detailed = images = 0
    flags, categories, shops, prices = Counter(), Counter(), {}, []
    for row, data in rows_for(db, params, "current", deadline, include_facts=False):
        total += 1
        detailed += int(data["detail_collected"])
        images += int(bool(data.get("image_url")))
        flags.update({key: int(value) for key, value in coverage_flags(data).items()})
        categories[row.category] += 1
        shops[str(row.shop_id)] = data["shop_name"]
        if row.price_min is not None:
            prices.append(row.price_min)
    fields = [{"field": key, "label": label, "known_count": flags[key], "total_count": total,
               "coverage_pct": percent(flags[key], total)} for key, label in FIELD_LABELS.items()]
    fields.append({"field": "image", "label": "真实商品图片", "known_count": images, "total_count": total,
                   "coverage_pct": percent(images, total), "ready": True})
    return {"total": total, "field_coverage": fields,
        "categories": [{"category": key, "count": count} for key, count in sorted(categories.items(), key=lambda pair: (-pair[1], pair[0]))[:GROUP_LIMIT]],
        "categories_truncated": len(categories) > GROUP_LIMIT,
        "shops": [{"id": key, "name": value} for key, value in sorted(shops.items())[:SHOP_LIMIT]],
        "shops_truncated": len(shops) > SHOP_LIMIT,
        "summary": {"products": total, "detail_products": detailed, "price_stats": price_statistics(prices)},
        "generated_at": stamp.isoformat()}


def build_snapshot(db, config, kind, params):
    stamp, deadline = now(db), time.monotonic() + config.analytics_build_budget_seconds
    if kind == "workbench":
        payload = build_current(db, params, stamp, deadline)
    elif kind == "catalog_stats":
        payload = build_catalog_stats(db, params, stamp, deadline)
    elif kind == "overview":
        payload = build_overview(db, params, stamp, deadline)
    elif kind == "events_summary":
        payload = event_stats(db, params, stamp)
    elif kind == "task_counts":
        from app.services.scheduler import task_counts
        payload = {"tasks": [{"task_type": str(k), "status": str(s), "count": n} for k, s, n in task_counts(db, config)]}
    elif kind == "matching_candidates":
        from app.services.matching_service import candidate_matches
        payload = candidate_matches(db, config, product_id=params.get("product_id"), limit=20)
    else:
        raise ValueError("unknown_analytics_kind")
    if time.monotonic() > deadline:
        raise TimeoutError("analytics_build_budget")
    pending, oldest = db.execute(select(func.count(), func.min(Job.pending_since)).where(Job.schema_version == VERSION,
        Job.is_pending == true(), Job.kind.in_(("product_projection", "event_projection")),
        Job.requested_revision > Job.completed_revision)).one()
    payload["_source"] = {"source_pending_count": pending,
        "oldest_pending_age_seconds": max(0, int((stamp - oldest).total_seconds())) if oldest else 0}
    return json_value(payload), stamp


def build_snapshots(db, config, kind, parameters):
    """Share window-independent facts within the same repeatable-read view."""
    deadline = time.monotonic() + config.analytics_build_budget_seconds
    first = parameters[0]
    if len(parameters) > 1 and (kind not in {"workbench", "overview"} or any(
        {k: v for k, v in params.items() if k != "lookback_hours"} !=
        {k: v for k, v in first.items() if k != "lookback_hours"} for params in parameters)):
        raise ValueError("incompatible_statistics_batch")
    payload, stamp = build_snapshot(db, config, kind, first)
    results = [(payload, stamp)]
    for params in parameters[1:]:
        result = deepcopy(payload)
        events = event_stats(db, params, stamp, product_filtered=kind == "workbench")
        result["summary"]["events_in_window"] = events["events_in_window"]
        if kind == "workbench":
            result.update(scope=params, **{key: events[key] for key in ("window", "events_by_type", "event_trend_daily")})
        else:
            result.update(since=events["window"]["since"], events=events["events"], event_buckets=events["event_buckets"],
                          events_truncated=events["events_in_window"] > len(events["events"]))
        results.append((json_value(result), stamp))
        if time.monotonic() > deadline:
            raise TimeoutError("analytics_build_budget")
    return results
