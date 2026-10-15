"""Requeue selected failures after a verified fix; retain every old attempt."""

import argparse
import json

from sqlalchemy import select, func

from app.models import CrawlAttempt, CrawlTask, TaskStatus
from app.services.task_runtime import database_now
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory, session_scope


def main():
    parser = argparse.ArgumentParser(description="在已验证修复后重试指定错误，不清空失败历史")
    parser.add_argument("--error-code", action="append", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--extra-attempts", type=int, default=0, help="已验证修复后，为耗尽的任务追加最多三次尝试；保留旧次数")
    args = parser.parse_args()
    allowed = {"product_url_identity_mismatch", "access_limited", "invalid_product_text",
               "sku_property_identity_conflict", "invalid_sku_price", "product_api_business_error", "lease_expired",
               "pagination_not_progressing"}
    if not set(args.error_code).issubset(allowed): raise ValueError("此入口仅允许已核实的兼容问题或已恢复的租约故障。")
    if not 0 <= args.extra_attempts <= 3: raise ValueError("修复后的追加尝试需要在 0-3 次之间。")
    config = Settings.load()
    engine = make_engine(config.database_url)
    try:
        with session_scope(make_session_factory(engine)) as db:
            latest = select(CrawlAttempt.task_id, func.max(CrawlAttempt.attempt_no).label("attempt_no")).group_by(CrawlAttempt.task_id).subquery()
            tasks = db.scalars(select(CrawlTask).join(latest, latest.c.task_id == CrawlTask.id).join(CrawlAttempt,
                (CrawlAttempt.task_id == CrawlTask.id) & (CrawlAttempt.attempt_no == latest.c.attempt_no)).where(
                CrawlTask.platform == config.platform, CrawlTask.region == config.region,
                CrawlTask.status.in_((TaskStatus.DEAD, TaskStatus.RETRY_WAITING)),
                CrawlAttempt.error_type.in_(args.error_code), CrawlTask.lease_token.is_(None),
                CrawlTask.attempt_count < CrawlTask.max_attempts + args.extra_attempts).with_for_update()).all()
            identifiers = [task.task_id for task in tasks]
            if args.execute:
                for task in tasks:
                    if task.attempt_count >= task.max_attempts:
                        task.max_attempts += args.extra_attempts
                    task.status, task.available_at, task.finished_at = TaskStatus.RETRY_WAITING, database_now(db), None
                    recovery = "verified_runtime_recovery" if set(args.error_code) == {"lease_expired"} else "verified_compatibility_fix"
                    if set(args.error_code) == {"pagination_not_progressing"}:
                        recovery = "verified_source_recovery"
                    task.checkpoint = {**(task.checkpoint or {}), "operator_recovery": recovery}
            print(json.dumps({"executed": args.execute, "selected_task_count": len(identifiers),
                              "selected_tasks": identifiers[:20], "task_ids_truncated": len(identifiers) > 20,
                              "old_attempts_preserved": True}))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
