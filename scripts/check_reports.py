"""Read-only report API and immutable artifact verification on a loopback service."""

import argparse
import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from app.models import CrawlTask, TaskStatus, TaskType
from app.settings import Settings
from app.storage.database import PROJECT_ROOT, make_engine, make_session_factory
from scripts.check_runtime import database_state


def main():
    parser = argparse.ArgumentParser(description="只读核对报告接口、队列产物与检查前后数据库状态")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data/samples/report-check/api-check.json")
    args = parser.parse_args()
    url = urlsplit(args.api_url)
    if (url.scheme != "http" or url.hostname != "127.0.0.1" or url.username or url.password
            or url.query or url.fragment or url.path not in {"", "/"}):
        raise ValueError("检查只允许无凭据的回环 HTTP 服务。")
    config = Settings.load()
    engine = make_engine(config.database_url)
    try:
        before = database_state(engine)
        with make_session_factory(engine)() as db:
            task = db.scalar(select(CrawlTask).where(CrawlTask.task_id == args.task_id,
                CrawlTask.task_type == TaskType.REPORT_GENERATION, CrawlTask.platform == config.platform,
                CrawlTask.region == config.region))
            if task is None or task.status != TaskStatus.SUCCEEDED:
                raise ValueError("需要本市场已完成的报告任务。")
            checkpoint, attempts = task.checkpoint, task.attempt_count
        checks = {}
        with httpx.Client(timeout=20, trust_env=False) as client:
            base = args.api_url.rstrip("/")
            for extension in ("json", "html", "md"):
                route = f"/api/v1/reports/current?format={extension}"
                response = client.get(base + route)
                checks[route] = response.status_code
                if response.status_code != 200: raise ValueError("报告只读接口未成功。")
                if extension == "json": current = response.json()
                route = f"/api/v1/reports/jobs/{args.task_id}/files/{extension}"
                response = client.get(base + route)
                checks[route] = response.status_code
                artifact = Path(checkpoint["files"][extension]).resolve()
                if not artifact.is_relative_to(config.report_directory.resolve()):
                    raise ValueError("报告产物超出指定导出目录。")
                if response.status_code != 200 or response.content != artifact.read_bytes():
                    raise ValueError("报告下载与任务产物不一致。")
                if extension == "json":
                    saved = response.json()
                    if sha256(response.content).hexdigest() != checkpoint["report_hash"]:
                        raise ValueError("报告 JSON 摘要不一致。")
            for route, expected in (("/api/v1/reports/current?lookback_hours=0", 422),
                                    ("/api/v1/reports/current?format=csv", 422),
                                    ("/api/v1/reports/jobs/missing/files/html", 404)):
                response = client.get(base + route)
                checks[route] = response.status_code
                if response.status_code != expected: raise ValueError("报告输入或下载边界检查失败。")
        after = database_state(engine)
        if before != after: raise ValueError("只读检查期间数据库状态发生变化。")
        evidence = {"checked_at": datetime.now(timezone.utc).isoformat(), "http_status": checks,
                    "task_id": args.task_id, "task_attempt_count": attempts, "report_hash": checkpoint["report_hash"],
                    "report_as_of": saved["as_of"], "saved_summary": saved["summary"], "current_summary": current["summary"],
                    "downloads_match_task_files": True, "database_unchanged_during_check": True,
                    "database_state": after, "check_started_collection": False}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"http_checks_passed": len(checks), "database_unchanged": True,
                          "report": str(args.output)}, ensure_ascii=False))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "报告只读核查未完成。"}, ensure_ascii=False))
        raise SystemExit(1)
