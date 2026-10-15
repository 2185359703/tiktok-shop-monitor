"""Read-only loopback HTTP/database checks, with a sanitized evidence report."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select

from app.crawlers.public_shop import shop_id_from_url
from app.models import Base, CrawlTask, Shop
from app.services.seed_importer import read_seed_csv
from app.storage.database import PROJECT_ROOT, make_engine


def database_state(engine):
    with engine.connect() as db:
        counts = {table.name: db.scalar(select(func.count()).select_from(table))
                  for table in Base.metadata.sorted_tables}
        tasks = db.execute(select(CrawlTask.task_id, CrawlTask.status, CrawlTask.attempt_count,
                                  CrawlTask.available_at, CrawlTask.lease_token, CrawlTask.updated_at)
                           .order_by(CrawlTask.id)).all()
        digest = hashlib.sha256(json.dumps([list(row) for row in tasks], default=str,
                                          separators=(",", ":")).encode()).hexdigest()
    return {"table_counts": counts, "task_state_hash": digest}


def main():
    parser = argparse.ArgumentParser(description="只读验证本机 API、现有任务和种子一致性")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data/samples/runtime/architecture-check.json")
    args = parser.parse_args()
    parsed = urlsplit(args.api_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("检查只允许无凭据的本机回环 HTTP 服务。")
    engine = make_engine()
    try:
        before = database_state(engine)
        routes = ["/health/live", "/health/ready", "/api/v1/capabilities", "/api/v1/overview",
                  "/api/v1/shops?limit=100", "/api/v1/products?limit=1", "/api/v1/tasks?limit=1",
                  "/api/v1/events?limit=1", "/metrics", "/openapi.json"]
        checks = {}
        with httpx.Client(timeout=10, trust_env=False) as client:
            for route in routes:
                response = client.get(args.api_url.rstrip("/") + route)
                checks[route] = response.status_code
                if response.status_code != 200:
                    raise ValueError("只读 HTTP 检查失败。")
                if route == "/api/v1/capabilities" and response.json()["enabled_task_types"]:
                    raise ValueError("本次检查要求网站采集开关关闭。")
        batch = read_seed_csv(PROJECT_ROOT / "data/seed/shops_us_clothing.csv")
        identifiers = [shop_id_from_url(str(row.normalized["shop_url"])) for row in batch.rows]
        if None in identifiers or len(set(identifiers)) != len(identifiers):
            raise ValueError("当前种子入口身份不完整或重复。")
        with engine.connect() as db:
            stored = dict(db.execute(select(Shop.platform_shop_id, Shop.canonical_url).where(
                Shop.platform == "tiktok_shop", Shop.region == "US", Shop.platform_shop_id.in_(identifiers))).all())
        aligned = all(stored.get(shop_id_from_url(str(row.normalized["shop_url"]))) == row.normalized["shop_url"]
                      for row in batch.rows)
        after = database_state(engine)
        report = {"checked_at": datetime.now(timezone.utc).isoformat(), "http_status": checks,
                  "current_seed_count": len(batch.rows), "distinct_shop_ids": len(set(identifiers)),
                  "seed_urls_match_database": aligned, "database_unchanged_during_check": before == after,
                  "database_state": after, "rabbitmq_broker_verified": False,
                  "docker_stack_started": False, "check_started_collection": False}
        if not aligned or before != after:
            raise ValueError("种子/数据库或检查前后任务状态不一致。")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"read_only_http_checks": len(checks), "seed_count": len(batch.rows),
                          "database_unchanged": before == after, "report": str(args.output)}, ensure_ascii=False))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # No exception strings or response bodies, which may contain credentials.
        print(json.dumps({"error_type": type(error).__name__, "message": "只读验证未完成，请检查本机服务和配置。"}, ensure_ascii=False))
        raise SystemExit(1)
