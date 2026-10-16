"""Repeat UI reads against a newly-created LOCAL synthetic MySQL database.

MYSQL_TEST_URL must name a new tiktok_shop_monitor_test_* database. The script
removes only that database on exit. It never accepts a business database.
"""

import argparse
import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import event, insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.main import create_app
from app.models import Shop
from app.models.analytics_read import AnalyticsRefreshRequest, ProductReadModel
from app.services.analytics_refresh import VERSION, job_row, key_for, scope
from app.services.analytics_rollup import backfill_rollups
from app.services.analytics_snapshot import build_catalog_stats, build_current, build_overview, publish_snapshot
from app.settings import Settings
from app.storage.database import make_engine
from app.storage.schema import mysql_ddl


def seed(factory, count):
    stamp = datetime.now(timezone.utc)
    with factory.begin() as db:
        db.execute(insert(Shop), [dict(id=i, platform="tiktok_shop", region="US", platform_shop_id=str(i), canonical_name=f"Shop {i:02}",
            canonical_url=f"https://example.invalid/shop/{i}", currency="USD", created_at=stamp, updated_at=stamp,
            first_seen_at=stamp, last_seen_at=stamp) for i in range(1, 41)])
    for first in range(1, count + 1, 256):
        values = []
        for i in range(first, min(first + 256, count + 1)):
            shop, category = (i % 40) + 1, ("Dresses" if i % 3 else "Tops")
            price = None if i % 7 == 0 else Decimal(i % 10000) / 100
            seen = stamp - timedelta(hours=i % 300)
            payload = dict(id=str(i), shop_id=str(shop), title=f"Dress sample {i:06}", normalized_category=category,
                original_category=category, detail_collected=i % 4 != 0, price_min=str(price) if price is not None else None,
                price_max=str(price) if price is not None else None, stock_status="unknown" if i % 4 == 0 else "in_stock",
                last_success_at=seen.isoformat() if i % 4 else None, rating="4.50" if i % 3 else None, review_count=i % 100,
                discount_rate=None if i % 5 else "0.1", color_summary=["Red", "Blue"], size_summary=["S", "M"],
                brand=f"Brand {i % 30}", material="Cotton", image_url="https://example.invalid/" + "x" * 900,
                thumbnail_url="https://example.invalid/" + "x" * 900, currency="USD", pricing_context="public_listed",
                variant_count=2 if i % 4 else None, product_url=f"https://example.invalid/p/{i}")
            facts = dict(sku_states={"in_stock": 1, "unknown": 1}, sku_cells=[["Red", "S", "in_stock", 1]],
                         history_count=i % 4, history_observations=i % 4, history_first=seen.isoformat() if i % 4 else None,
                         history_last=stamp.isoformat() if i % 4 else None)
            row = dict(schema_version=VERSION, product_id=i, platform="tiktok_shop", region="US", currency="USD",
                pricing_context="public_listed", shop_id=shop, platform_product_id=str(i), title=payload["title"], category=category,
                price_min=price, stock_status=payload["stock_status"], sku_count=payload["variant_count"],
                rating=Decimal(payload["rating"]) if payload["rating"] else None, review_count=payload["review_count"],
                observed_at=seen, eligible=True, applied_revision=1, projected_at=stamp)
            values.append({**row, "basis": "current", "payload": payload, "facts": facts})
            values.append({**row, "basis": "latest_valid_snapshot", "facts": {}, "payload": dict(payload,
                snapshot_id=str(i) if i % 4 else None, sku_count=2, sku_in_stock=1, sku_out_of_stock=0,
                sku_unknown=1, price_kind="unknown" if price is None else "exact", category=category)})
        with factory.begin() as db:
            db.execute(insert(ProductReadModel), values)
    print(json.dumps({"phase": "seeded", "products": count}), flush=True)
    return stamp


def measure(factory, config, args):
    marker_params = {**scope(config), "name": "synthetic_ui_benchmark"}
    if args.reuse_fixture:
        with factory() as db:
            marker = db.get(AnalyticsRefreshRequest, (VERSION, "control", key_for("control", marker_params)))
            if marker is None or marker.parameters.get("products") != args.products:
                raise ValueError("Existing database is not this script's matching synthetic fixture")
            timings = marker.parameters["builds"]
        return measure_http(factory, config, args, timings)
    stamp = seed(factory, args.products)
    params = scope(config)
    builders = {"workbench": build_current, "overview": build_overview, "catalog_stats": build_catalog_stats}
    baseline, timings = {}, {}
    with factory() as db:
        for kind, build in builders.items():
            started = time.perf_counter()
            baseline[kind] = build(db, params, stamp, time.monotonic() + 300)
            timings[kind] = {"full_scan_seconds": round(time.perf_counter() - started, 3)}
    started, last_log = time.monotonic(), time.monotonic()
    while True:
        with factory.begin() as db:
            done = backfill_rollups(db, config)
        if done:
            break
        if time.monotonic() - last_log > 20:
            print(json.dumps({"phase": "rollup_backfill", "elapsed_seconds": round(time.monotonic() - started)}), flush=True)
            last_log = time.monotonic()
    with factory.begin() as db:
        for kind, build in builders.items():
            started = time.perf_counter()
            value = build(db, params, stamp, time.monotonic() + 300)
            timings[kind]["rollup_seconds"] = round(time.perf_counter() - started, 3)
            if value != baseline[kind]:
                raise AssertionError(f"{kind} differs from full-scope result")
            query = {**params, **({"lookback_hours": 24} if kind != "catalog_stats" else {})}
            publish_snapshot(db, kind, query, 1, value, datetime.now(timezone.utc))
        control = job_row(db, "control", {**params, "name": "backfill"})
        control.parameters = {**control.parameters, "ready": True}
        marker = job_row(db, "control", marker_params)
        marker.parameters = {**marker_params, "products": args.products, "builds": timings}
    print(json.dumps({"phase": "summaries_verified", "timings": timings}), flush=True)
    return measure_http(factory, config, args, timings)


def measure_http(factory, config, args, timings):
    paths = ["/api/v2/catalog/products?page=1", "/api/v2/catalog/products?page=500",
        "/api/v2/catalog/products?page=1900", "/api/v2/catalog/products?sort=price_asc&page=100",
        "/api/v2/catalog/products?sort=shop_asc&page=100", "/api/v2/catalog/products?sort=title&page=100",
        "/api/v2/catalog/products?shop_id=20&page=10", "/api/v2/catalog/products?category=Dresses&page=20",
        "/api/v2/catalog/products?q=Dress&sort=price_desc&page=20",
        "/api/v2/catalog/products?attribute=color&value=Red&page=20",
        "/api/v2/analytics/overview", "/api/v2/analytics/workbench?topic=market",
        "/api/v2/analytics/workbench?topic=attributes", "/api/v2/catalog/stats"]
    measurements = {}
    os.environ["ANALYTICS_ENABLED"] = "1"
    app = create_app(config)
    with TestClient(app) as client:
        statements = []
        def capture(conn, cursor, statement, params, *unused):
            if statement.startswith("SELECT") and "product_read_models" in statement:
                statements.append((statement, params))
        for path in paths:
            durations, sizes, statuses = [], [], []
            statements.clear()
            event.listen(app.state.engine.sync_engine, "before_cursor_execute", capture)
            for sample in range(args.samples):
                started = time.perf_counter()
                response = client.get(path)
                durations.append((time.perf_counter() - started) * 1000)
                sizes.append(len(response.content))
                statuses.append(response.status_code)
                if sample == 0:
                    event.remove(app.state.engine.sync_engine, "before_cursor_execute", capture)
                if response.status_code != 200:
                    break
            ordered = sorted(durations)
            measurements[path] = dict(samples=len(statuses), p50_ms=round(ordered[len(ordered) // 2], 2),
                p95_ms=round(ordered[math.ceil(len(ordered) * .95) - 1], 2), max_bytes=max(sizes), statuses=sorted(set(statuses)))
            plans = []
            with factory() as db:
                for statement, params in statements:
                    raw = db.connection().exec_driver_sql("EXPLAIN FORMAT=JSON " + statement, params).scalar()
                    plans.append(json.loads(raw))
            measurements[path]["plans"] = plans
            print(json.dumps({"path": path, **{k: v for k, v in measurements[path].items() if k != "plans"}}), flush=True)
    return {"observed_at": datetime.now(timezone.utc).isoformat(), "products": args.products,
        "source": "deterministic synthetic data in disposable local MySQL; native async FastAPI; Redis disabled",
        "correctness": "all three complete rollup outputs equal the full-scan outputs",
        "builds": timings, "http": measurements}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--products", type=int, default=50000, choices=range(1, 100001))
    parser.add_argument("--samples", type=int, default=20, choices=range(20, 101))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--keep-fixture", action="store_true", help="Keep this script's isolated fixture for query-plan diagnosis")
    parser.add_argument("--reuse-fixture", action="store_true", help="Only remeasure an existing fixture marked by this script")
    args = parser.parse_args()
    url = make_url(os.environ["MYSQL_TEST_URL"])
    name = url.database or ""
    if url.host not in {"127.0.0.1", "localhost"} or not re.fullmatch(r"tiktok_shop_monitor_test_[A-Za-z0-9_]+", name):
        raise ValueError("Only a new local tiktok_shop_monitor_test_* database is allowed")
    admin, engine, created = make_engine(url, with_database=False), None, False
    try:
        with admin.connect() as db:
            exists = db.scalar(text("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=:name"), {"name": name})
            if exists and not args.reuse_fixture:
                raise ValueError("Database already exists; choose a new test name")
            if args.reuse_fixture and not exists:
                raise ValueError("The marked fixture does not exist")
            for statement in (() if exists else mysql_ddl(name).split(";")):
                sql = "\n".join(line for line in statement.splitlines() if not line.lstrip().startswith("--")).strip()
                if sql:
                    db.exec_driver_sql(sql)
                    created |= sql.startswith("CREATE DATABASE")
        engine = make_engine(url)
        result = measure(sessionmaker(engine), Settings(database_url=url.render_as_string(hide_password=False)), args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return int(any(item["statuses"] != [200] for item in result["http"].values()))
    finally:
        if engine is not None:
            engine.dispose()
        if created and not args.keep_fixture:
            with admin.connect() as db:
                db.exec_driver_sql(f"DROP DATABASE {name}")
        admin.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
