"""Bounded manual execution of already-discovered public product detail tasks."""

import argparse
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import String, cast, func, select

from app.models import CrawlTask, Product, ResourceType, TaskStatus, TaskType
from app.observability.logging import configure_logging
from app.observability.metrics import Metrics
from app.queue.contracts import TaskMessage
from app.queue.redis_gate import RedisGate
from app.settings import Settings
from app.storage.database import PROJECT_ROOT, make_engine, make_session_factory
from app.workers.handlers import handler_registry
from app.workers.runtime import Worker


def main():
    parser = argparse.ArgumentParser(description="详情与 SKU 小批量补全，默认只读展示计划")
    parser.add_argument("--product-id", action="append", default=[], help="平台商品 ID，可重复；不是内部主键")
    parser.add_argument("--max-products", type=int, default=3)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.max_products <= 100: raise ValueError("小批量预算必须为 1-100。")
    configure_logging()
    settings = replace(Settings.load(), enabled_task_types=(TaskType.PRODUCT_DETAIL,))
    engine, gate = make_engine(settings.database_url), None
    factory = make_session_factory(engine)
    try:
        with factory() as db:
            query = select(CrawlTask, Product).join(Product, CrawlTask.resource_id == cast(Product.id, String)).where(
                CrawlTask.platform == settings.platform, CrawlTask.region == settings.region,
                Product.platform == settings.platform, Product.region == settings.region,
                CrawlTask.task_type == TaskType.PRODUCT_DETAIL, CrawlTask.resource_type == ResourceType.PRODUCT,
                CrawlTask.status.in_((TaskStatus.PENDING, TaskStatus.RETRY_WAITING, TaskStatus.ABANDONED)),
                CrawlTask.available_at <= func.utc_timestamp(6), CrawlTask.lease_token.is_(None), CrawlTask.attempt_count < CrawlTask.max_attempts)
            if args.product_id: query = query.where(Product.platform_product_id.in_(args.product_id))
            rows = db.execute(query.order_by(Product.id, CrawlTask.id)).all()
            chosen = {}
            for task, product in rows:
                if product.platform_product_id not in chosen and len(chosen) < args.max_products:
                    chosen[product.platform_product_id] = {"task_id": task.task_id, "product_id": str(product.id),
                                                          "platform_product_id": product.platform_product_id}
        if not args.execute:
            print(json.dumps({"dry_run": True, "products": list(chosen.values())}, ensure_ascii=False, indent=2))
            return 0
        if not settings.redis_url: raise ValueError("实跑需要 REDIS_URL 共享限速与暂停。")
        gate = RedisGate(settings)
        worker = Worker(settings, factory, handler_registry(), gate, Metrics(), f"detail-pilot:{uuid4().hex[:12]}")
        report = {"started_at": datetime.now(timezone.utc).isoformat(), "mode": "local_sql_worker_pilot", "rabbitmq_used": False, "products": []}
        failed = False
        for entry in chosen.values():
            print(json.dumps({"event": "read_product_detail", **entry}), flush=True)
            action = worker.process(TaskMessage(entry["task_id"], TaskType.PRODUCT_DETAIL))
            with factory() as db:
                task = db.scalar(select(CrawlTask).where(CrawlTask.task_id == entry["task_id"]))
                report["products"].append({**entry, "status": task.status.value, "worker_action": action, **(task.checkpoint or {})})
                failed = task.status != TaskStatus.SUCCEEDED
            if failed or gate.pause_remaining(): break
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["summary"] = {"details_succeeded": sum(row["status"] == "succeeded" for row in report["products"]),
                             "variants_seen": sum(row.get("variants_seen", 0) for row in report["products"]),
                             "new_variants": sum(row.get("new_variants", 0) for row in report["products"]),
                             "attribute_conflicts": sum(row.get("attribute_conflicts", 0) for row in report["products"]), "failed": failed}
        output = args.output or PROJECT_ROOT / f"data/samples/product-detail/pilot-{uuid4().hex[:12]}.json"
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
        print(json.dumps({"error_type": type(error).__name__, "message": "详情实跑未完成，请检查配置与任务状态。"}, ensure_ascii=False))
        raise SystemExit(1)
