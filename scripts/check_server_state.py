"""Persist a sanitized, read-only deployment and collection snapshot."""

import json
import re
from datetime import datetime, timezone

import httpx
from sqlalchemy import func, select, text

from app.models import Base
from app.services.collection_service import collection_progress
from app.services.seed_importer import read_seed_csv
from app.settings import Settings
from app.storage.database import PROJECT_ROOT, make_engine, make_session_factory


def main():
    config = Settings.load()
    engine = make_engine(config.database_url)
    try:
        with make_session_factory(engine)() as db:
            counts = {table.name: db.scalar(select(func.count()).select_from(table)) for table in Base.metadata.sorted_tables}
            comments = db.execute(text("SELECT TABLE_NAME, TABLE_COMMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='BASE TABLE'")).all()
            columns = db.execute(text("SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()")).all()
            progress = collection_progress(db, read_seed_csv(PROJECT_ROOT / "data/seed/shops_us_clothing.csv"), require_baseline=config.baseline_on_detail)
            version = db.scalar(text("SELECT VERSION()"))
        checks = {}
        # The complete current report is paged in SQL and can take a minute on
        # the small VPS. This is a diagnostic timeout, not a healthcheck timeout.
        with httpx.Client(timeout=120, trust_env=False) as client:
            for route in ("/", "/health/live", "/health/ready", "/api/v1/capabilities", "/api/v1/overview",
                          "/api/v1/collections/current", "/api/v1/reports/current", "/api/v1/tasks?task_type=report_generation&limit=10", "/metrics"):
                response = client.get("http://api:8000" + route)
                checks[route] = response.status_code
            targets = client.get("http://prometheus:9090/api/v1/targets").json()["data"]["activeTargets"]
        evidence = {"checked_at": datetime.now(timezone.utc).isoformat(), "checks_are_read_only": True,
                    "mysql_version": version, "tables": len(comments), "columns": len(columns),
                    "all_table_comments_chinese": all(re.search(r"[\u4e00-\u9fff]", comment or "") for _, comment in comments),
                    "all_column_comments_chinese": all(re.search(r"[\u4e00-\u9fff]", comment or "") for _, _, comment in columns),
                    "table_counts": counts, "http_status": checks, "progress": progress,
                    "prometheus_targets": [{"job": target["labels"].get("job"), "health": target["health"]} for target in targets],
                    "runtime": {"baseline_on_detail": config.baseline_on_detail, "detail_http_backend": config.detail_http_backend,
                                "detail_proxy_configured": bool(config.detail_proxy_url), "resource_document_cooldown": config.resource_document_cooldown,
                                "periodic_snapshots_enabled": config.periodic_snapshots_enabled}}
        if any(status != 200 for status in checks.values()):
            raise ValueError("某个部署只读接口未成功。")
        # Running from the API's read-only volume cannot save; emit the evidence
        # for the caller to save on the host instead.
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
