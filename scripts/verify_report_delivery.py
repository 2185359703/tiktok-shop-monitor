"""Read-only, streamed checks of deployed report responses and task artifacts."""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import httpx
from sqlalchemy import select

from app.models import CrawlTask, TaskStatus, TaskType
from app.reports.exporter import file_hash
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory


BASE = "http://api:8000"


def stream_check(route):
    began, digest, size = time.monotonic(), sha256(), 0
    with httpx.Client(timeout=180, trust_env=False) as client:
        with client.stream("GET", BASE + route) as response:
            if response.status_code != 200:
                raise ValueError("报告响应不是成功状态。")
            generated_at = response.headers.get("x-report-generated-at")
            expected = response.headers.get("content-length")
            for chunk in response.iter_bytes(chunk_size=65536):
                digest.update(chunk)
                size += len(chunk)
            if expected is not None and int(expected) != size:
                raise ValueError("报告响应长度不一致。")
    return {"status": 200, "seconds": round(time.monotonic() - began, 3), "bytes": size,
            "sha256": digest.hexdigest(), "generated_at": generated_at}


def main():
    parser = argparse.ArgumentParser(description="核验实际阶段报告、并发实时响应与热缓存，不触发站点采集")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--artifacts-only", action="store_true", help="仅核对不可变产物，不触发实时报告重算")
    args = parser.parse_args()
    config = Settings.load()
    engine = make_engine(config.database_url)
    try:
        with make_session_factory(engine)() as db:
            task = db.scalar(select(CrawlTask).where(CrawlTask.task_id == args.task_id,
                CrawlTask.task_type == TaskType.REPORT_GENERATION, CrawlTask.platform == config.platform,
                CrawlTask.region == config.region))
            if task is None or task.status != TaskStatus.SUCCEEDED:
                raise ValueError("需要本市场成功的阶段报告。")
            checkpoint, attempts = dict(task.checkpoint), task.attempt_count
        current, warm = None, None
        if not args.artifacts_only:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {kind: pool.submit(stream_check, "/api/v1/reports/current?format=" + kind)
                           for kind in ("json", "md")}
                current = {kind: future.result() for kind, future in futures.items()}
            warm = stream_check("/api/v1/reports/current?format=json")
            if (not current["json"]["generated_at"] or current["json"]["generated_at"] != current["md"]["generated_at"]
                    or warm["generated_at"] != current["json"]["generated_at"]
                    or warm["sha256"] != current["json"]["sha256"]):
                raise ValueError("同上下文的并发响应或热缓存未保持同一报告。")
        artifacts = {}
        for kind in ("json", "html", "md"):
            path = Path(checkpoint["files"][kind]).resolve()
            if not path.is_relative_to(config.report_directory.resolve()) or not path.is_file():
                raise ValueError("报告文件不在受控导出目录。")
            checked = stream_check(f"/api/v1/reports/jobs/{args.task_id}/files/{kind}")
            if checked["sha256"] != file_hash(path):
                raise ValueError("HTTP下载与已登记文件不一致。")
            artifacts[kind] = {"filename": path.name, **checked}
        if artifacts["json"]["sha256"] != checkpoint["report_hash"]:
            raise ValueError("JSON产物与任务摘要不一致。")
        print(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(), "read_only": True,
            "task_id": args.task_id, "task_attempt_count": attempts, "report_as_of": checkpoint["report_as_of"],
            "report_products": checkpoint["report_products"], "report_hash": checkpoint["report_hash"],
            "concurrent_current_responses": current, "warm_current_response": warm,
            "shared_generation_and_warm_hash_match": True if current else None, "artifacts": artifacts,
            "all_downloads_match_registered_files": True}, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "完整报告交付核验未通过，未打印凭据或原始报告。"}, ensure_ascii=False))
        raise SystemExit(1)
