"""A bounded local pilot using the real worker lifecycle, without RabbitMQ."""

import argparse
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import String, cast, func, select

from app.crawlers.public_shop import shop_id_from_url
from app.models import CrawlTask, ResourceType, Shop, TaskStatus, TaskType
from app.observability.logging import configure_logging
from app.observability.metrics import Metrics
from app.queue.contracts import TaskMessage
from app.queue.redis_gate import RedisGate
from app.services.seed_importer import read_seed_csv
from app.settings import Settings
from app.storage.database import PROJECT_ROOT, make_engine, make_session_factory
from app.workers.handlers import handler_registry
from app.workers.runtime import Worker


def ready_tasks_query(settings, platform_shop_ids):
    return select(CrawlTask, Shop).join(Shop, cast(Shop.id, String) == CrawlTask.resource_id).where(
        CrawlTask.platform == settings.platform, CrawlTask.region == settings.region,
        Shop.platform == settings.platform, Shop.region == settings.region,
        Shop.platform_shop_id.in_(platform_shop_ids), CrawlTask.resource_type == ResourceType.SHOP,
        CrawlTask.task_type == TaskType.PRODUCT_DISCOVERY,
        CrawlTask.status.in_((TaskStatus.PENDING, TaskStatus.RETRY_WAITING, TaskStatus.ABANDONED)),
        CrawlTask.available_at <= func.utc_timestamp(6), CrawlTask.lease_token.is_(None),
        CrawlTask.attempt_count < CrawlTask.max_attempts,
    ).order_by(Shop.id, CrawlTask.id)


def main():
    parser = argparse.ArgumentParser(description="少量真实店铺分页实跑；默认仅展示计划，不访问网站")
    parser.add_argument("--shop-id", action="append", default=[], help="已在当前种子中的平台店铺 ID，可重复")
    parser.add_argument("--max-shops", type=int, default=3)
    parser.add_argument("--pages-per-shop", type=int, default=2)
    parser.add_argument("--execute", action="store_true", help="实际领取选中任务、访问网站并写入商品")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.max_shops <= 100 or not 1 <= args.pages_per_shop <= 500:
        raise ValueError("店铺/页数预算超出范围。")
    configure_logging()
    settings = replace(Settings.load(), enabled_task_types=(TaskType.PRODUCT_DISCOVERY,))
    current = read_seed_csv(PROJECT_ROOT / "data/seed/shops_us_clothing.csv")
    current_ids = {shop_id_from_url(str(row.normalized["shop_url"])) for row in current.rows}
    requested = set(args.shop_id)
    if None in current_ids or requested - current_ids:
        raise ValueError("仅能执行当前已核验种子的店铺。")
    identifiers = requested or current_ids
    engine = make_engine(settings.database_url)
    factory, gate = make_session_factory(engine), None
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "execution_mode": "local_sql_worker_pilot",
              "rabbitmq_used": False, "pages_per_shop_budget": args.pages_per_shop, "shops": []}
    try:
        with factory() as db:
            planned = db.execute(ready_tasks_query(settings, identifiers)).all()
            chosen = {}
            for task, shop in planned:
                if shop.platform_shop_id not in chosen and len(chosen) < args.max_shops:
                    chosen[shop.platform_shop_id] = {"shop_id": str(shop.id), "platform_shop_id": shop.platform_shop_id,
                                                   "shop_name": shop.canonical_name, "task_id": task.task_id}
        if not args.execute:
            print(json.dumps({"dry_run": True, "shops": list(chosen.values()), "pages_per_shop": args.pages_per_shop}, ensure_ascii=False, indent=2))
            return 0
        if not settings.redis_url:
            raise ValueError("实跑需要配置 REDIS_URL，以共享请求间隔和市场暂停。")
        gate = RedisGate(settings)
        worker = Worker(settings, factory, handler_registry(), gate, Metrics(), f"product-pilot:{uuid4().hex[:12]}")
        failed = False
        for entry in chosen.values():
            item = {**entry, "pages": []}
            report["shops"].append(item)
            task_id = entry["task_id"]
            for _ in range(args.pages_per_shop):
                print(json.dumps({"event": "read_product_page", "shop_name": entry["shop_name"], "task_id": task_id}, ensure_ascii=False), flush=True)
                action = worker.process(TaskMessage(task_id, TaskType.PRODUCT_DISCOVERY))
                with factory() as db:
                    task = db.scalar(select(CrawlTask).where(CrawlTask.task_id == task_id))
                    progress = dict(task.checkpoint or {})
                    item["pages"].append({"task_id": task_id, "status": task.status.value, "worker_action": action, **progress})
                    if task.status != TaskStatus.SUCCEEDED:
                        failed = True
                        break
                    following = db.scalar(ready_tasks_query(settings, [entry["platform_shop_id"]]).with_only_columns(CrawlTask).where(
                        CrawlTask.payload["parent_task_id"].as_string() == task_id).limit(1))
                    if following is None:
                        break
                    task_id = following.task_id
            if failed or gate.pause_remaining():
                break
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        pages = [page for shop in report["shops"] for page in shop["pages"]]
        report["summary"] = {"shops_processed": len(report["shops"]), "pages_succeeded": sum(page["status"] == "succeeded" for page in pages),
                             "new_products": sum(page.get("new_products", 0) for page in pages),
                             "new_detail_tasks": sum(page.get("new_detail_tasks", 0) for page in pages),
                             "whole_shop_traversal_completed": sum(bool(shop["pages"] and shop["pages"][-1].get("complete")) for shop in report["shops"]),
                             "failed": failed}
        output = args.output or PROJECT_ROOT / f"data/samples/product-discovery/pilot-{uuid4().hex[:12]}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({**report["summary"], "report": str(output)}, ensure_ascii=False, indent=2))
        return 1 if failed else 0
    finally:
        if gate:
            gate.close()
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "商品实跑未完成，检查本地配置和任务状态。"}, ensure_ascii=False))
        raise SystemExit(1)
