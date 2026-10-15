"""Resume one fixed seed batch, account for coverage, and queue its final report."""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from sqlalchemy import select, text

from app.models import CrawlTask, TaskStatus, TaskType
from app.observability.logging import configure_logging
from app.services.collection_service import collection_progress
from app.services.report_service import ensure_report_task
from app.services.seed_importer import fingerprint, import_seed_batch, read_seed_csv
from app.settings import Settings
from app.storage.database import PROJECT_ROOT, make_engine, make_session_factory, session_scope
from scripts.runtime_support import stop_signal


def save_state(directory, state):
    directory.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=directory, prefix=".collection-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, directory / "current.json")
    finally:
        if temporary is not None and temporary.exists(): temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description="按固定种子名单持续执行全量采集，结束后生成报告")
    parser.add_argument("--seed-file", type=Path, default=PROJECT_ROOT / "data/seed/shops_us_clothing.csv")
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--status", action="store_true", help="只查询本批覆盖与队列，不启动采集")
    args = parser.parse_args()
    if not 5 <= args.poll_seconds <= 300: raise ValueError("状态刷新间隔需要在 5-300 秒之间。")
    configure_logging()
    config, batch = Settings.load(), read_seed_csv(args.seed_file)
    if any(row.normalized["region"] != config.region or not row.normalized["shop_url"] for row in batch.rows):
        raise ValueError("全量采集名单必须为本市场的公开店铺入口。")
    engine = make_engine(config.database_url)
    factory = make_session_factory(engine)
    try:
        if args.status:
            with factory() as db:
                print(json.dumps(collection_progress(db, batch, require_baseline=config.baseline_on_detail), ensure_ascii=False, indent=2))
            return 0
        required = {TaskType.SHOP_DISCOVERY, TaskType.PRODUCT_DISCOVERY, TaskType.PRODUCT_DETAIL,
                    TaskType.CHANGE_DETECTION, TaskType.REPORT_GENERATION}
        if not required.issubset(config.enabled_task_types):
            raise ValueError("全量控制进程需要显式启用店铺、列表、详情、变化检测和报告处理器。")
        config.require_infrastructure()
        lock_name = "tiktok_shop_full_collection_US"
        with engine.connect() as lock:
            if lock.scalar(text("SELECT GET_LOCK(:name, 0)"), {"name": lock_name}) != 1:
                raise ValueError("该市场已有全量任务控制进程。")
            try:
                seed_hash = fingerprint({"row_hashes": [row.row_hash for row in batch.rows]})
                path = config.collection_directory / "current.json"
                if path.exists():
                    state = json.loads(path.read_text(encoding="utf-8"))
                    if state.get("seed_hash") != seed_hash or state.get("platform") != config.platform or state.get("region") != config.region:
                        raise ValueError("已有批次范围不同，不能覆盖其进度。")
                else:
                    state = {"job_id": str(uuid4()), "platform": config.platform, "region": config.region,
                             "seed_hash": seed_hash, "started_at": datetime.now(timezone.utc).isoformat(),
                             "status": "running", "report_generation": 0}
                    save_state(config.collection_directory, state)
                with session_scope(factory) as db:
                    import_seed_batch(db, batch)
                stop = stop_signal()
                while not stop.is_set():
                    # Detect lost lock connections before allowing another write.
                    if lock.scalar(text("SELECT IS_USED_LOCK(:name) = CONNECTION_ID()"), {"name": lock_name}) != 1:
                        raise RuntimeError("全量任务控制锁已失效。")
                    with factory() as db:
                        progress = collection_progress(db, batch, require_baseline=config.baseline_on_detail)
                    if progress["active_tasks"] or progress["seed_observations"] != progress["target_shops"]:
                        if state.get("status") in {"completed", "partial"}:
                            state.pop("report_task_id", None)
                            state.pop("finished_at", None)
                            state["report_generation"] += 1
                        state["status"] = "running"
                    else:
                        if not state.get("report_task_id"):
                            window = datetime.fromisoformat(state["started_at"]) + timedelta(microseconds=state["report_generation"])
                            with session_scope(factory) as db:
                                report, _ = ensure_report_task(db, config, window)
                                state["report_task_id"] = report.task_id
                        state["status"] = "report_pending"
                        with factory() as db:
                            report = db.scalar(select(CrawlTask).where(CrawlTask.task_id == state["report_task_id"]))
                            state["report_status"] = report.status.value
                            if report.status == TaskStatus.SUCCEEDED:
                                state["status"] = "completed" if progress["full_coverage"] else "partial"
                                state["report_hash"] = report.checkpoint["report_hash"]
                            elif report.status in {TaskStatus.DEAD, TaskStatus.CANCELLED}:
                                state["status"] = "partial"
                    state.update(progress, updated_at=datetime.now(timezone.utc).isoformat())
                    if state["status"] in {"completed", "partial"}:
                        state.setdefault("finished_at", state["updated_at"])
                    save_state(config.collection_directory, state)
                    print(json.dumps({key: state.get(key) for key in ("job_id", "status", "source_complete_shops", "target_shops",
                          "products", "details_completed", "variants", "active_tasks", "failed_tasks")}, ensure_ascii=False), flush=True)
                    if state["status"] in {"completed", "partial"}: return 0
                    stop.wait(args.poll_seconds)
            finally:
                lock.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name})
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "全量采集控制进程异常，进度与任务保留。"}, ensure_ascii=False))
        raise SystemExit(1)
