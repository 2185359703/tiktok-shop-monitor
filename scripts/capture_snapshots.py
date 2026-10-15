"""Bounded genuine re-observation plus local comparisons; default is read-only."""

import argparse
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, func

from app.models import CrawlTask, Product, ResourceType, TaskStatus, TaskType
from app.observability.logging import configure_logging
from app.observability.metrics import Metrics
from app.queue.contracts import TaskMessage
from app.queue.redis_gate import RedisGate
from app.services.snapshot_service import ensure_snapshot_task, utc_window
from app.services.task_runtime import database_now
from app.settings import Settings
from app.storage.database import PROJECT_ROOT, make_engine, make_session_factory, session_scope
from app.workers.handlers import handler_registry
from app.workers.runtime import Worker


def main():
    parser = argparse.ArgumentParser(description="少量商品历史快照与变化检测，默认只读计划")
    parser.add_argument("--product-id", action="append", default=[], help="平台商品 ID，可重复")
    parser.add_argument("--max-products", type=int, default=3)
    parser.add_argument("--scheduled-for", help="带时区的 ISO 时间；相同窗口重复执行不覆盖历史")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.max_products <= 100: raise ValueError("采集预算必须为 1-100。")
    configure_logging()
    settings = replace(Settings.load(), enabled_task_types=(TaskType.PRODUCT_SNAPSHOT, TaskType.CHANGE_DETECTION))
    engine, gate = make_engine(settings.database_url), None
    factory = make_session_factory(engine)
    try:
        with factory() as db:
            window = datetime.fromisoformat(args.scheduled_for) if args.scheduled_for else utc_window(database_now(db), settings.snapshot_interval_seconds)
            if window.tzinfo is None: raise ValueError("窗口必须有显式时区。")
            window = window.astimezone(timezone.utc)
            if window > database_now(db): raise ValueError("手动实跑不能选择未来窗口。")
            query = select(Product).where(Product.platform == settings.platform, Product.region == settings.region,
                                           Product.last_success_at.is_not(None), Product.pricing_context == "public_listed")
            if args.product_id: query = query.where(Product.platform_product_id.in_(args.product_id))
            products = db.scalars(query.order_by(Product.id).limit(args.max_products)).all()
            plan = [{"product_id": str(product.id), "platform_product_id": product.platform_product_id} for product in products]
        if not args.execute:
            print(json.dumps({"dry_run": True, "scheduled_for": window.isoformat(), "products": plan}, ensure_ascii=False, indent=2))
            return 0
        if not settings.redis_url: raise ValueError("真实重采需要 Redis 共享请求闸门。")
        gate = RedisGate(settings)
        worker = Worker(settings, factory, handler_registry(), gate, Metrics(), f"snapshot-pilot:{uuid4().hex[:12]}")
        report = {"scheduled_for": window.isoformat(), "started_at": datetime.now(timezone.utc).isoformat(),
                  "mode": "local_sql_worker_pilot", "rabbitmq_used": False, "snapshots": [], "comparisons": []}
        failed = False
        for item in plan:
            with session_scope(factory) as db:
                product = db.scalar(select(Product).where(Product.id == int(item["product_id"])).with_for_update())
                task, _ = ensure_snapshot_task(db, product, window)
                task_id, before_attempts = task.task_id, task.attempt_count
            action = worker.process(TaskMessage(task_id, TaskType.PRODUCT_SNAPSHOT))
            with factory() as db:
                saved = db.scalar(select(CrawlTask).where(CrawlTask.task_id == task_id))
                report["snapshots"].append({**item, "task_id": task_id, "status": saved.status.value,
                                           "worker_action": action, "executed_this_run": saved.attempt_count > before_attempts,
                                           **(saved.checkpoint or {})})
                failed = saved.status != TaskStatus.SUCCEEDED
            if failed or gate.pause_remaining(): break
        # Comparisons use stored observations and still work during a website pause.
        product_ids = [item["product_id"] for item in plan]
        with factory() as db:
            due = db.scalars(select(CrawlTask).where(CrawlTask.platform == settings.platform, CrawlTask.region == settings.region,
                CrawlTask.task_type == TaskType.CHANGE_DETECTION, CrawlTask.resource_type == ResourceType.PRODUCT,
                CrawlTask.resource_id.in_(product_ids), CrawlTask.status.in_((TaskStatus.PENDING, TaskStatus.RETRY_WAITING, TaskStatus.ABANDONED)),
                CrawlTask.available_at <= func.utc_timestamp(6), CrawlTask.lease_token.is_(None))
                .order_by(CrawlTask.id).limit(2 * args.max_products)).all()
            comparisons = [task.task_id for task in due]
        for task_id in comparisons:
            worker.process(TaskMessage(task_id, TaskType.CHANGE_DETECTION))
            with factory() as db:
                task = db.scalar(select(CrawlTask).where(CrawlTask.task_id == task_id))
                report["comparisons"].append({"task_id": task_id, "status": task.status.value, **(task.checkpoint or {})})
                failed = failed or task.status != TaskStatus.SUCCEEDED
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["summary"] = {"snapshots_succeeded": sum(item["status"] == "succeeded" for item in report["snapshots"]),
                             "snapshots_written_this_run": sum(item.get("snapshots_written", 0) for item in report["snapshots"] if item["executed_this_run"]),
                             "comparisons_succeeded": sum(item["status"] == "succeeded" for item in report["comparisons"]),
                             "events_written": sum(item.get("events_written", 0) for item in report["comparisons"]), "failed": failed}
        output = args.output or PROJECT_ROOT / f"data/samples/snapshots/pilot-{uuid4().hex[:12]}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({**report["summary"], "report": str(output)}, ensure_ascii=False, indent=2))
        return 1 if failed else 0
    finally:
        if gate: gate.close()
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "快照实跑未完成，请检查任务状态和本地配置。"}, ensure_ascii=False))
        raise SystemExit(1)
