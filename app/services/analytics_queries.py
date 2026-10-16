"""Bounded UI reads over narrow projections, without catalog-wide statistics."""

import base64
import json
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, literal, or_, select, true, union_all

from app.models import Shop
from app.models.analytics_read import AnalyticsRefreshRequest, EventReadModel as E, ProductReadModel as P
from app.services.analytics_refresh import SCOPE_FIELDS, VERSION, key_for, now, scope


def parameters(config, options=None, **extra):
    values = {**scope(config), **{k: v for k, v in (options or {}).items() if v is not None and v != ""}, **extra}
    values["currency"] = str(values["currency"]).upper()
    if values.get("q"):
        values["q"] = values["q"].strip()
        if not values["q"]:
            values.pop("q")
    if not values.get("price_upper_exclusive"):
        values.pop("price_upper_exclusive", None)
    for field in ("price_min", "price_max"):
        if values.get(field) is not None:
            values[field] = format(Decimal(str(values[field])).quantize(Decimal("0.01")), "f")
    return json.loads(json.dumps(values, default=str))


def product_conditions(params, basis="current"):
    clauses = [P.schema_version == VERSION, P.basis == basis, P.eligible == true(),
               *(getattr(P, k) == params[k] for k in ("platform", "region", "currency", "pricing_context")),
               Shop.platform == params["platform"], Shop.region == params["region"]]
    if params.get("shop_id"):
        clauses.append(P.shop_id == int(params["shop_id"]))
    if params.get("category"):
        clauses.append(P.category == params["category"])
    if params.get("q"):
        clauses.append(or_(*(col.contains(params["q"], autoescape=True) for col in (
            P.title, P.brand, P.platform_product_id, Shop.canonical_name))))
    for key, operator in (("price_min", "ge"), ("price_max", "le")):
        if params.get(key) is not None:
            value = Decimal(str(params[key]))
            clauses.append(P.price_min >= value if operator == "ge" else
                           P.price_min < value if params.get("price_upper_exclusive") else P.price_min <= value)
    if params.get("price_unknown") is not None:
        clauses.append(P.price_min.is_(None) if params["price_unknown"] else P.price_min.is_not(None))
    if params.get("stock"):
        clauses.append(P.stock_status == params["stock"])
    if params.get("attribute"):
        attribute, value = params["attribute"], params["value"]
        if attribute in {"color", "size"}:
            # JSON membership retains exact labels; SQL dialect handling is in the caller.
            clauses.append(P.payload[("color_summary" if attribute == "color" else "size_summary")].is_not(None))
        else:
            clauses.append(func.trim(P.payload[attribute].as_string()) == value)
    return clauses


def product_query(db, params, basis="current"):
    query = select(P, Shop.canonical_name).join(Shop, Shop.id == P.shop_id).where(*product_conditions(params, basis))
    if params.get("attribute") in {"color", "size"}:
        column = P.payload["color_summary" if params["attribute"] == "color" else "size_summary"]
        # Reuse the dialect's JSON table helper; filter before LIMIT, never in Python after pagination.
        from app.services.analytics_service import json_values
        values = json_values(db, column)
        from sqlalchemy import true
        matching = select(P.product_id).select_from(P).join(values, true()).where(
            P.schema_version == VERSION, P.basis == basis, func.trim(values.c.value) == params["value"])
        query = query.where(P.product_id.in_(matching))
    return query


def list_products(db, params, *, page=1, page_size=24, sort="recent"):
    sorts = {"recent": (P.observed_at.desc(),), "price_asc": (P.price_min.asc(),), "price_desc": (P.price_min.desc(),),
        "title": (P.title.asc(),), "title_desc": (P.title.desc(),), "shop_asc": (Shop.canonical_name.asc(),),
        "shop_desc": (Shop.canonical_name.desc(),), "stock_asc": (P.stock_status.asc(),),
        "stock_desc": (P.stock_status.desc(),), "sku_count_asc": (P.sku_count.asc(),),
        "sku_count_desc": (P.sku_count.desc(),), "rating_desc": (P.rating.desc(),), "reviews_desc": (P.review_count.desc(),)}
    if sort not in sorts or page < 1 or not 1 <= page_size <= 100:
        raise ValueError("invalid_pagination")
    order = sorts[sort]
    if sort == "price_asc":
        order = (P.price_min.is_(None), *order)
    if sort.startswith("stock_"):
        order = (P.stock_status == "unknown", *order)
    if sort == "sku_count_asc":
        order = (P.sku_count.is_(None), *order)
    # Match both directions of the recent index; a mixed-direction tie break
    # makes MySQL sort the entire catalog before returning the first page.
    descending = sort == "recent" or sort.endswith("_desc")
    tie_break = P.product_id.desc() if descending else P.product_id.asc()
    query = product_query(db, params).with_only_columns(P.product_id.label("page_product_id"))
    if not (set(params) - set(SCOPE_FIELDS)):
        # Scope columns are correlated; MySQL can underestimate their combined
        # selectivity and switch back to a catalog filesort on the second page.
        index = {"recent": "ix_read_products_recent", "title": "ix_read_products_title",
                 "title_desc": "ix_read_products_title", "price_desc": "ix_read_products_price",
                 "sku_count_desc": "ix_read_products_sku"}.get(sort, "ix_read_products_catalog")
        query = query.with_hint(P, f"FORCE INDEX ({index})", dialect_name="mysql")
    offset, count = (page - 1) * page_size, page_size + 1
    if sort in {"price_asc", "sku_count_asc", "stock_asc", "stock_desc"}:
        column, index = ((P.price_min, "ix_read_products_price") if sort == "price_asc" else
            (P.sku_count, "ix_read_products_sku") if sort == "sku_count_asc" else (P.stock_status, "ix_read_products_stock"))
        unknown = column == "unknown" if sort.startswith("stock_") else column.is_(None)
        known = column != "unknown" if sort.startswith("stock_") else column.is_not(None)
        base = product_query(db, params).with_only_columns(P.product_id.label("product_id"), column.label("sort_value"))
        if not (set(params) - set(SCOPE_FIELDS)):
            base = base.with_hint(P, f"FORCE INDEX ({index})", dialect_name="mysql")
        value_order = column.desc() if descending else column.asc()
        # Each branch contributes at most the requested prefix. Sorting their
        # scalar keys preserves unknown-last paging without a catalog COUNT or
        # a filesort of every product on the first page.
        known_rows = base.where(known).order_by(value_order, tie_break).limit(offset + count).subquery()
        unknown_rows = base.where(unknown).order_by(tie_break).limit(offset + count).subquery()
        merged = union_all(select(known_rows.c.product_id, known_rows.c.sort_value, literal(0).label("unknown")),
            select(unknown_rows.c.product_id, unknown_rows.c.sort_value, literal(1).label("unknown"))).subquery()
        identifiers = db.scalars(select(merged.c.product_id).order_by(merged.c.unknown,
            merged.c.sort_value.desc() if descending else merged.c.sort_value.asc(),
            merged.c.product_id.desc() if descending else merged.c.product_id.asc()).offset(offset).limit(count)).all()
    elif sort in {"shop_asc", "shop_desc"}:
        # Rank the small shop directory once; sorting a numeric rank avoids
        # repeating large collation keys for every product of the same shop.
        ranks = select(Shop.id.label("shop_id"), func.dense_rank().over(
            order_by=Shop.canonical_name.desc() if descending else Shop.canonical_name.asc()).label("shop_rank")) \
            .where(Shop.platform == params["platform"], Shop.region == params["region"]).subquery()
        if not (set(params) - set(SCOPE_FIELDS)):
            shop_query = select(P.product_id).select_from(P).join(ranks, ranks.c.shop_id == P.shop_id).where(
                P.schema_version == VERSION, P.basis == "current", P.eligible == true(),
                *(getattr(P, key) == params[key] for key in SCOPE_FIELDS)).with_hint(
                    P, "FORCE INDEX (ix_read_products_catalog)", dialect_name="mysql")
        else:
            shop_query = query.join(ranks, ranks.c.shop_id == P.shop_id)
        identifiers = db.scalars(shop_query.prefix_with("STRAIGHT_JOIN", dialect="mysql")
            .order_by(ranks.c.shop_rank, tie_break).offset(offset).limit(count)).all()
    else:
        identifiers = db.scalars(query.order_by(*order, tie_break).offset(offset).limit(count)).all()
    # The caller's repeatable-read view keeps both queries consistent. Fetch
    # payloads by primary key after sorting; never filesort the JSON values.
    rows = db.execute(select(P.product_id, P.payload, Shop.canonical_name).join(Shop, Shop.id == P.shop_id)
        .where(P.schema_version == VERSION, P.basis == "current", P.product_id.in_(identifiers[:page_size]))).all() if identifiers else []
    by_id = {identifier: {**payload, "shop_name": name} for identifier, payload, name in rows}
    return {"items": [by_id[identifier] for identifier in identifiers[:page_size] if identifier in by_id],
            "page": page, "page_size": page_size, "has_more": len(identifiers) > page_size,
            "total": None, "total_pages": None, "scope": params, "field_coverage": [], "image_readiness": True}


def event_query(params):
    query = select(E, Shop.canonical_name).join(Shop, Shop.id == E.shop_id).where(E.schema_version == VERSION,
        *(getattr(E, k) == params[k] for k in ("platform", "region", "currency", "pricing_context")),
        Shop.platform == params["platform"], Shop.region == params["region"])
    if params.get("shop_id"):
        query = query.where(E.shop_id == int(params["shop_id"]))
    if params.get("event_type"):
        query = query.where(E.event_type == params["event_type"])
    if params.get("q"):
        query = query.where(or_(E.title.contains(params["q"], autoescape=True),
                               Shop.canonical_name.contains(params["q"], autoescape=True),
                               E.payload["platform_product_id"].as_string().contains(params["q"], autoescape=True)))
    return query


def decode_cursor(value):
    try:
        if len(value) > 4096:
            raise ValueError
        result = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("invalid_cursor") from None


def list_events(db, config, params, *, cursor=None, limit=20):
    filter_hash = key_for("event_filter", params)
    if cursor:
        saved = decode_cursor(cursor)
        try:
            if saved["filter"] != filter_hash:
                raise ValueError
            start, end, last = [datetime.fromisoformat(saved[k]) for k in ("since", "until", "last_time")]
            upper, last_id = int(saved["upper"]), int(saved["last_id"])
            if any(t.tzinfo is None for t in (start, end, last)) or start > end or upper < 0 or last_id < 1:
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise ValueError("invalid_cursor") from None
    else:
        end = now(db)
        start = end - timedelta(hours=int(params.get("lookback_hours", 24)))
        control = db.get(AnalyticsRefreshRequest, (VERSION, "control", key_for("control", {**scope(config), "name": "event_sequence"})))
        upper = int(control.parameters.get("sequence", 0)) if control else 0
        last = last_id = None
    query = event_query(params).where(E.observed_at >= start, E.observed_at <= end, E.published_sequence <= upper)
    if last is not None:
        query = query.where(or_(E.observed_at < last, and_(E.observed_at == last, E.event_id < last_id)))
    rows = db.execute(query.order_by(E.observed_at.desc(), E.event_id.desc()).limit(limit + 1)).all()
    next_cursor = None
    if len(rows) > limit:
        tail = rows[limit - 1][0]
        saved = dict(filter=filter_hash, since=start.isoformat(), until=end.isoformat(), upper=str(upper),
                     last_time=tail.observed_at.isoformat(), last_id=str(tail.event_id))
        next_cursor = base64.urlsafe_b64encode(json.dumps(saved, separators=(",", ":")).encode()).decode().rstrip("=")
    return {"items": [{**row.payload, "shop_name": name} for row, name in rows[:limit]], "next_cursor": next_cursor,
            "window": {"since": start.isoformat(), "until": end.isoformat(), "feed_upper_sequence": str(upper)}}
