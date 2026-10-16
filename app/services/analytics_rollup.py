"""Transactional, bounded product summaries for the default dashboard.

Each shard holds at most 128 products for one observation basis. A publication
replaces just its members and recomputes that small summary. Dashboard refreshes
merge summaries, retaining every price, literal attribute and observed SKU count.
"""

import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import delete, insert, select, tuple_
from sqlalchemy.orm import undefer

from app.models.analytics_read import AnalyticsProductRollup as Rollup, AnalyticsRefreshRequest as Job, ProductReadModel as P, ProductReadAttribute as Attribute
from app.services.analytics_refresh import VERSION, SCOPE_FIELDS, SUMMARY_KINDS, enqueue, job_row, key_for, now, scope
from app.services.analytics_service import SCATTER_LIMIT


SHARD_SIZE = 128
ROLLUP_VERSION = 1
ATTRIBUTES = {"colors": "color_summary", "sizes": "size_summary", "materials": "material", "styles": "style",
              "seasons": "season", "gender": "gender", "brands": "brand"}


def coverage_flags(data):
    has = lambda key: bool(str(data.get(key) or "").strip())
    return {"title": has("title"), "category": has("normalized_category") or has("original_category"),
        "price": data.get("price_min") is not None, "stock": data.get("stock_status") != "unknown",
        "discount": data.get("discount_rate") is not None, "rating": data.get("rating") is not None,
        "reviews": data.get("review_count") is not None, "colors": bool(data.get("color_summary")),
        "sizes": bool(data.get("size_summary")), **{key: has(key) for key in ("material", "brand", "style", "season", "gender")}}


def group():
    return {"prices": [], "count": 0, "detail": 0, "skus": Counter()}


@dataclass
class CurrentTotals:
    total: int = 0
    detailed: int = 0
    images: int = 0
    prices: list = field(default_factory=list)
    scatter: list = field(default_factory=list)
    eligible_scatter: int = 0
    groups: dict = field(default_factory=dict)
    categories: dict = field(default_factory=dict)
    flags: Counter = field(default_factory=Counter)
    stocks: Counter = field(default_factory=Counter)
    discounts: Counter = field(default_factory=Counter)
    seen: Counter = field(default_factory=Counter)
    attributes: dict = field(default_factory=lambda: {key: Counter() for key in ATTRIBUTES})
    known_attributes: Counter = field(default_factory=Counter)
    sku_states: Counter = field(default_factory=Counter)
    cells: Counter = field(default_factory=Counter)
    history_count: int = 0
    history_products: int = 0
    repeated: int = 0
    first_history: str | None = None
    last_history: str | None = None

    def add(self, row, data):
        self.total += 1
        price = Decimal(data["price_min"]) if data.get("price_min") is not None else None
        if price is not None:
            self.prices.append(price)
        self.detailed += int(data["detail_collected"])
        self.images += int(bool(data.get("image_url")))
        self.flags.update({k: int(v) for k, v in coverage_flags(data).items()})
        self.stocks[data["stock_status"]] += 1
        discount = Decimal(data["discount_rate"]) if data.get("discount_rate") is not None else None
        self.discounts["unknown" if discount is None else "none" if discount == 0 else "under10" if discount <= Decimal(".1") else
                       "10to30" if discount <= Decimal(".3") else "30to50" if discount <= Decimal(".5") else "over50"] += 1
        self.seen[data.get("last_success_at") or ""] += 1
        states = Counter(row.facts.get("sku_states", {}))
        self.sku_states.update(states)
        for color, size, status, count in row.facts.get("sku_cells", []):
            self.cells[(color, size, status)] += count
        for mapping, key in ((self.groups, str(row.shop_id)), (self.categories, row.category)):
            target = mapping.setdefault(key, group())
            target["count"] += 1
            target["detail"] += int(data["detail_collected"])
            target["skus"].update(states)
            if price is not None:
                target["prices"].append(price)
        for key, source in ATTRIBUTES.items():
            value = data.get(source)
            self.known_attributes[key] += int(bool(value) if isinstance(value, list) else bool(str(value or "").strip()))
            values = value if isinstance(value, list) else [value]
            self.attributes[key].update({str(v).strip() for v in values if v is not None and str(v).strip()})
        if price is not None and data.get("rating") is not None:
            self.eligible_scatter += 1
            if len(self.scatter) < SCATTER_LIMIT:
                self.scatter.append({k: data.get(k) for k in ("id", "title", "shop_id", "price_min", "rating", "review_count")})
        facts = row.facts
        self.history_count += facts.get("history_count", 0)
        self.history_products += int(facts.get("history_count", 0) > 0)
        self.repeated += int(facts.get("history_observations", 0) >= 2)
        self.history_bounds(facts.get("history_first"), facts.get("history_last"))

    def history_bounds(self, first, last):
        if first:
            self.first_history = min(self.first_history or first, first)
            self.last_history = max(self.last_history or last, last)

    def encode(self):
        from app.services.analytics_projection import json_value
        return json_value({**vars(self), "cells": [[*key, count] for key, count in self.cells.items()]})

    def merge(self, value):
        for key in ("total", "detailed", "images", "eligible_scatter", "history_count", "history_products", "repeated"):
            setattr(self, key, getattr(self, key) + value[key])
        self.prices.extend(Decimal(p) for p in value["prices"])
        self.scatter = sorted(self.scatter + value["scatter"], key=lambda p: int(p["id"]))[:SCATTER_LIMIT]
        for key in ("flags", "stocks", "discounts", "seen", "known_attributes", "sku_states"):
            getattr(self, key).update(value[key])
        for key in ATTRIBUTES:
            self.attributes[key].update(value["attributes"][key])
        for color, size, status, count in value["cells"]:
            self.cells[(color, size, status)] += count
        for key in ("groups", "categories"):
            for name, source in value[key].items():
                target = getattr(self, key).setdefault(name, group())
                target["prices"].extend(Decimal(p) for p in source["prices"])
                target["count"] += source["count"]
                target["detail"] += source["detail"]
                target["skus"].update(source["skus"])
        self.history_bounds(value["first_history"], value["last_history"])

    def freshness(self, stamp):
        counts = Counter()
        for seen, count in self.seen.items():
            age = (stamp - datetime.fromisoformat(seen)).total_seconds() if seen else None
            counts["never" if age is None else "under24h" if age <= 86400 else "1to3d" if age <= 259200 else
                   "3to7d" if age <= 604800 else "over7d"] += count
        return counts


@dataclass
class HistoricalTotals:
    totals: Counter = field(default_factory=Counter)
    stocks: Counter = field(default_factory=Counter)
    kinds: Counter = field(default_factory=Counter)
    prices: list = field(default_factory=list)
    shops: dict = field(default_factory=dict)
    categories: dict = field(default_factory=dict)

    def add(self, row, data):
        count = int(data["snapshot_id"] is not None)
        self.totals.update(products=1, historical_detail_products=count)
        target = self.shops.setdefault(str(row.shop_id), {"prices": [], "counts": Counter()})
        target["counts"].update(sampled_products=1, historical_detail_products=count, sku_count=data["sku_count"],
            sku_out_of_stock=data["sku_out_of_stock"], known_stock_skus=data["sku_in_stock"] + data["sku_out_of_stock"])
        self.stocks.update({key: data[key] for key in ("sku_in_stock", "sku_out_of_stock", "sku_unknown")})
        self.kinds[data["price_kind"]] += 1
        category = self.categories.setdefault(data["category"], {"count": 0, "prices": []})
        category["count"] += 1
        if data["price_min"] is not None:
            price = Decimal(data["price_min"])
            self.prices.append(price)
            target["prices"].append(price)
            category["prices"].append(price)

    def encode(self):
        from app.services.analytics_projection import json_value
        return json_value(vars(self))

    def merge(self, value):
        for key in ("totals", "stocks", "kinds"):
            getattr(self, key).update(value[key])
        self.prices.extend(Decimal(p) for p in value["prices"])
        for key, source in value["shops"].items():
            target = self.shops.setdefault(key, {"prices": [], "counts": Counter()})
            target["prices"].extend(Decimal(p) for p in source["prices"])
            target["counts"].update(source["counts"])
        for key, source in value["categories"].items():
            target = self.categories.setdefault(key, {"count": 0, "prices": []})
            target["prices"].extend(Decimal(p) for p in source["prices"])
            target["count"] += source["count"]


def unfiltered(params):
    return not (set(params) - set(SCOPE_FIELDS) - {"lookback_hours"})


def control_params(config):
    return {**scope(config), "name": f"product_rollup_v{ROLLUP_VERSION}"}


def rollups_ready(db, config):
    row = db.get(Job, (VERSION, "control", key_for("control", control_params(config))))
    return bool(row and row.parameters.get("ready"))


def scope_key(params):
    return key_for("product_rollup", {k: params[k] for k in SCOPE_FIELDS})


def read_totals(db, params, basis, deadline):
    if not unfiltered(params) or not rollups_ready(db, SimpleNamespace(**{k: params[k] for k in SCOPE_FIELDS})):
        return None
    result = CurrentTotals() if basis == "current" else HistoricalTotals()
    key = scope_key(params)
    query = select(Rollup.summary).where(Rollup.schema_version == ROLLUP_VERSION, Rollup.basis == basis).order_by(Rollup.shard_id)
    for summary in db.scalars(query.execution_options(yield_per=32)):
        if time.monotonic() > deadline:
            raise TimeoutError("analytics_build_budget")
        if key in summary:
            result.merge(summary[key])
    return result


def compact(values):
    data = values["payload"]
    fields = ("id", "title", "shop_id", "price_min", "rating", "review_count", "detail_collected", "stock_status",
        "discount_rate", "last_success_at", "normalized_category", "original_category", *ATTRIBUTES.values()) if values["basis"] == "current" else (
        "snapshot_id", "sku_count", "sku_in_stock", "sku_out_of_stock", "sku_unknown", "price_kind", "price_min", "category")
    return {"scope": {key: values[key] for key in SCOPE_FIELDS}, "shop_id": values["shop_id"],
        "category": values["category"], "eligible": values["eligible"], "revision": values["applied_revision"],
        "data": {**{key: data.get(key) for key in fields}, "image_url": bool(data.get("image_url"))},
        "facts": values["facts"] if values["basis"] == "current" else {}}


def update_rollups(db, rows):
    batches = defaultdict(list)
    applied = []
    for values in rows:
        batches[(values["basis"], values["product_id"] // SHARD_SIZE)].append(values)
    for (basis, shard), values in sorted(batches.items()):
        initial = dict(schema_version=ROLLUP_VERSION, basis=basis, shard_id=shard, members={}, summary={})
        if db.bind.dialect.name == "mysql":
            from sqlalchemy.dialects.mysql import insert
            stmt = insert(Rollup).values(**initial)
            db.execute(stmt.on_duplicate_key_update(shard_id=stmt.inserted.shard_id))
        else:
            from sqlalchemy.dialects.sqlite import insert
            db.execute(insert(Rollup).values(**initial).on_conflict_do_nothing())
        row = db.scalar(select(Rollup).options(undefer(Rollup.members)).where(Rollup.schema_version == ROLLUP_VERSION,
            Rollup.basis == basis, Rollup.shard_id == shard).with_for_update().execution_options(populate_existing=True))
        members = dict(row.members)
        for value in values:
            identifier = str(value["product_id"])
            if identifier not in members or members[identifier]["revision"] <= value["applied_revision"]:
                members[identifier] = compact(value)
                applied.append(value)
        summaries = {}
        for identifier, member in sorted(members.items(), key=lambda pair: int(pair[0])):
            if not member["eligible"]:
                continue
            key = scope_key(member["scope"])
            state = summaries.setdefault(key, CurrentTotals() if basis == "current" else HistoricalTotals())
            state.add(SimpleNamespace(shop_id=member["shop_id"], category=member["category"], facts=member["facts"]), member["data"])
        row.members, row.summary = members, {key: state.encode() for key, state in summaries.items()}
    update_attributes(db, applied)


def update_attributes(db, rows):
    if not rows:
        return
    identities = [(row["basis"], row["product_id"]) for row in rows]
    db.execute(delete(Attribute).where(Attribute.schema_version == VERSION,
        tuple_(Attribute.basis, Attribute.product_id).in_(identities)))
    values = []
    for row in rows:
        payload = row["payload"]
        for attribute in ("color", "size", "brand", "gender", "season", "style", "material"):
            raw = payload.get({"color": "color_summary", "size": "size_summary"}.get(attribute, attribute))
            members = raw if isinstance(raw, list) else [raw] if raw is not None else []
            labels = {str(value)[:512].strip(" ") if attribute in {"color", "size"} else str(value).strip(" ")
                      for value in members if value is not None}
            values.extend(dict(schema_version=VERSION, basis=row["basis"], product_id=row["product_id"],
                attribute=attribute, ordinal=ordinal, value=value) for ordinal, value in enumerate(sorted(labels)))
    for offset in range(0, len(values), 500):
        db.execute(insert(Attribute), values[offset:offset + 500])


def backfill_rollups(db, config):
    control = job_row(db, "control", control_params(config))
    if control.parameters.get("ready"):
        return True
    state = dict(control.parameters)
    identifiers = db.scalars(select(P.product_id).where(P.schema_version == VERSION, P.basis == "current",
        P.product_id > state.get("after", 0)).order_by(P.product_id).limit(SHARD_SIZE)).all()
    if not identifiers:
        control.parameters = {**state, "ready": True}
        from app.services.analytics_runtime import ready
        if ready(db, config):
            # An upgrade may inherit the previous release's 300-second due
            # times. Publish the newly available summaries immediately.
            stamp = now(db)
            for kind in SUMMARY_KINDS:
                for hours in ((24, 168, 720) if kind != "catalog_stats" else (None,)):
                    params = {**scope(config), **({"lookback_hours": hours} if hours else {})}
                    request = enqueue(db, kind, params)
                    request.available_at = stamp
        return True
    # The primary key is (schema_version, basis, product_id). Supplying only
    # schema_version + product_id makes MySQL scan every basis on every batch.
    rows = [row for basis in ("current", "latest_valid_snapshot") for row in db.scalars(
        select(P).options(undefer(P.facts)).where(P.schema_version == VERSION, P.basis == basis,
            P.product_id.in_(identifiers)).with_hint(P, "FORCE INDEX (PRIMARY)", dialect_name="mysql"))]
    update_rollups(db, [{column.name: getattr(row, column.name) for column in P.__table__.columns} for row in rows])
    control.parameters = {**state, "after": identifiers[-1]}
    return False
