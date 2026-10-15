"""Collect a bounded, explicit shop list through the normal leased Worker."""

import argparse
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.crawlers.public_shop import shop_id_from_url
from app.models import CrawlTask, ResourceType, Shop, TaskStatus, TaskType
from app.observability.logging import configure_logging
from app.observability.metrics import Metrics
from app.queue.contracts import TaskMessage
from app.queue.redis_gate import RedisGate
from app.services.seed_importer import read_seed_csv
from app.services.shop_profile import ensure_shop_profile_task, shop_profile_window
from app.services.task_runtime import database_now
from app.settings import Settings
from app.storage.database import PROJECT_ROOT, make_engine, make_session_factory, session_scope
from app.workers.handlers import handler_registry
from app.workers.runtime import Worker


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--shop-id", action="append", help="已入库的平台店铺 ID，可重复")
    source.add_argument("--seed-file", type=Path, help="已核验店铺 CSV，按真实公开链接限定范围")
    parser.add_argument("--max-shops", type=int, default=3)
    parser.add_argument("--scheduled-for", help="带时区的 ISO 观察窗口；重跑同一窗口不会覆盖历史")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.max_shops <= 100:
        raise ValueError("单次店铺预算必须为 1-100。")
    config = replace(Settings.load(), enabled_task_types=(TaskType.SHOP_PROFILE,),
                     shop_profiles_enabled=False, periodic_snapshots_enabled=False)
    identifiers = args.shop_id or [shop_id_from_url(row.normalized["shop_url"])
                                   for row in read_seed_csv(args.seed_file).rows
                                   if row.normalized["region"] == config.region]
    if not identifiers or any(not isinstance(value, str) or not value.isascii() or not value.isdecimal()
                              or len(value) > 128 for value in identifiers):
        raise ValueError("需要已核验的本市场店铺 ID 或公开店铺链接。")
    identifiers = list(dict.fromkeys(identifiers))[:args.max_shops]
    configure_logging()
    engine, gate = make_engine(config.database_url), None
    factory = make_session_factory(engine)
    try:
        with factory() as db:
            now = database_now(db)
            window = datetime.fromisoformat(args.scheduled_for) if args.scheduled_for else shop_profile_window(now, config.shop_profile_interval_seconds)
            if window.tzinfo is None or window > now:
                raise ValueError("实跑观察窗口必须带时区且不能晚于当前时间。")
            window = window.astimezone(timezone.utc)
            rows = db.scalars(select(Shop).where(Shop.platform == config.platform, Shop.region == config.region,
                                                Shop.platform_shop_id.in_(identifiers))).all()
            shops = {row.platform_shop_id: row for row in rows}
            if any(identifier not in shops or shop_id_from_url(shops[identifier].canonical_url or "") != identifier
                   for identifier in identifiers):
                raise ValueError("指定范围包含尚未入库或没有已核验入口的店铺。")
            plan = [{"id": str(shops[value].id), "platform_shop_id": value,
                     "name": shops[value].canonical_name} for value in identifiers]
        if not args.execute:
            print(json.dumps({"dry_run": True, "scheduled_for": window.isoformat(), "shops": plan}, ensure_ascii=False, indent=2))
            return 0
        if not config.redis_url:
            raise ValueError("真实采集需要现有 Redis 共享请求闸门。")
        gate = RedisGate(config)
        worker = Worker(config, factory, handler_registry(), gate, Metrics(), f"shop-profile:{uuid4().hex[:12]}")
        report = {"started_at": datetime.now(timezone.utc).isoformat(), "scheduled_for": window.isoformat(),
                  "mode": "leased_local_worker", "rabbitmq_used": False, "target_shops": len(plan), "shops": []}
        # Persist the complete explicit plan before HTTP. A shared pause leaves
        # the remaining targets visibly pending and resumable by a normal Worker.
        for item in plan:
            with session_scope(factory) as db:
                shop = db.scalar(select(Shop).where(Shop.id == int(item["id"])).with_for_update())
                ensure_shop_profile_task(db, shop, window)
                task = db.scalar(select(CrawlTask).where(
                    CrawlTask.platform == shop.platform, CrawlTask.region == shop.region,
                    CrawlTask.task_type == TaskType.SHOP_PROFILE, CrawlTask.resource_type == ResourceType.SHOP,
                    CrawlTask.resource_id == str(shop.id), CrawlTask.scheduled_for == window,
                ).order_by(CrawlTask.id).limit(1))
                item["task_id"] = task.task_id
        for item in plan:
            task_id = item["task_id"]
            if gate.pause_remaining():
                break
            with factory() as db:
                before_attempts = db.scalar(select(CrawlTask.attempt_count).where(CrawlTask.task_id == task_id))
            action = worker.process(TaskMessage(task_id, TaskType.SHOP_PROFILE))
            with factory() as db:
                task = db.scalar(select(CrawlTask).where(CrawlTask.task_id == task_id))
                shop = db.get(Shop, int(item["id"]))
                report["shops"].append({**item, "task_id": task_id, "status": task.status.value,
                    "worker_action": action, "executed_this_run": task.attempt_count > before_attempts,
                    "profile_captured_at": shop.profile_captured_at.isoformat() if shop.profile_captured_at else None,
                    "observation_status": (task.checkpoint or {}).get("observation_status"),
                    "follower_count": shop.follower_count, "sales_count": shop.sales_count,
                    "video_count": shop.video_count, "verification_type": shop.verification_type,
                    "identity_label_key": shop.identity_label_key, "checkpoint": task.checkpoint})
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        processed_ids = {item["id"] for item in report["shops"]}
        report["unprocessed_shops"] = [item for item in plan if item["id"] not in processed_ids]
        report["summary"] = {
            "target_shops": len(plan), "processed_shops": len(report["shops"]),
            "succeeded": sum(row["status"] == TaskStatus.SUCCEEDED.value for row in report["shops"]),
            "complete": sum(row["status"] == TaskStatus.SUCCEEDED.value and row["observation_status"] == "complete" for row in report["shops"]),
            "pending_or_failed": len(plan) - sum(row["status"] == TaskStatus.SUCCEEDED.value for row in report["shops"]),
        }
        output = args.output or PROJECT_ROOT / f"data/samples/shop-profile/run-{uuid4().hex[:12]}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({**report["summary"], "report": str(output.resolve())}, ensure_ascii=False, indent=2))
        return 1 if report["summary"]["pending_or_failed"] else 0
    finally:
        if gate:
            gate.close()
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "店铺档案采集未完成，请查看任务与本地配置；未输出连接信息。"}, ensure_ascii=False))
        raise SystemExit(1)
