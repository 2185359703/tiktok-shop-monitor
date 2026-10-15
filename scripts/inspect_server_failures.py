"""Read bounded runtime diagnostics without printing credentials or SQL bodies."""

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import case, func, select, text

from app.models import CrawlAttempt, CrawlTask, Product, TaskStatus
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory


def main():
    parser = argparse.ArgumentParser(description="限时只读检查任务失败与数据库执行状态")
    parser.add_argument("--queries-only", action="store_true")
    parser.add_argument("--cancel-stalled-progress", action="store_true",
                        help="仅取消本库本用户、超过60秒且匹配旧统计特征的SELECT，不删除任务")
    args = parser.parse_args()
    config = Settings.load()
    engine = make_engine(config.database_url)
    try:
        with make_session_factory(engine)() as db:
            # This applies only to this diagnostic connection, not the workers.
            db.execute(text("SET SESSION MAX_EXECUTION_TIME=10000"))
            if args.cancel_stalled_progress:
                candidates = db.execute(text("""
                    SELECT ID, INFO FROM information_schema.PROCESSLIST
                    WHERE USER=SUBSTRING_INDEX(USER(), '@', 1) AND DB=DATABASE()
                      AND COMMAND='Query' AND TIME > 60 AND ID <> CONNECTION_ID()
                """)).all()
                cancelled = []
                for identifier, sql in candidates:
                    normalized = " ".join((sql or "").casefold().split())
                    if (normalized.startswith("select crawl_tasks.task_type, crawl_tasks.status, count(")
                            and "in (select cast(products.id as char(128))" in normalized
                            and "crawl_tasks.platform = 'tiktok_shop'" in normalized
                            and "crawl_tasks.region = 'us'" in normalized):
                        db.execute(text(f"KILL QUERY {int(identifier)}"))
                        cancelled.append(identifier)
                print(json.dumps({"cancelled_old_read_only_progress_queries": cancelled,
                                  "task_rows_or_attempts_deleted": False}), flush=True)
                return
            processes = db.execute(text("""
                SELECT ID, COMMAND, TIME, STATE,
                    CASE
                        WHEN INFO LIKE '%crawl_tasks.task_type%GROUP BY%' THEN 'task_scope_aggregation'
                        WHEN INFO LIKE '%product_variants%' THEN 'variant_query'
                        WHEN INFO LIKE '%crawl_attempts%' THEN 'attempt_query'
                        WHEN INFO LIKE '%crawl_tasks%' THEN 'task_query'
                        ELSE 'other'
                    END AS query_kind
                FROM information_schema.PROCESSLIST
                WHERE COMMAND <> 'Sleep' AND ID <> CONNECTION_ID()
                ORDER BY TIME DESC
            """)).mappings().all()
            if args.queries_only:
                print(json.dumps({"read_only": True, "active_database_queries": [dict(row) for row in processes]},
                                 ensure_ascii=False, indent=2, default=str), flush=True)
                return
            market = (CrawlTask.platform == config.platform, CrawlTask.region == config.region)
            counts = db.execute(select(CrawlTask.task_type, CrawlTask.status, func.count())
                                .where(*market).group_by(CrawlTask.task_type, CrawlTask.status)).all()
            latest = select(CrawlAttempt.task_id, func.max(CrawlAttempt.attempt_no).label("attempt_no"))\
                .group_by(CrawlAttempt.task_id).subquery()
            failed = select(CrawlTask.task_id, CrawlTask.task_type, CrawlTask.status,
                            CrawlTask.attempt_count, CrawlTask.max_attempts, CrawlAttempt.error_type,
                            CrawlAttempt.response_status, CrawlAttempt.request_url, CrawlAttempt.evidence_path)\
                .join(latest, latest.c.task_id == CrawlTask.id).join(CrawlAttempt,
                    (CrawlAttempt.task_id == CrawlTask.id) & (CrawlAttempt.attempt_no == latest.c.attempt_no))\
                .where(*market, CrawlTask.status.in_((TaskStatus.DEAD, TaskStatus.RETRY_WAITING)))
            failures = failed.subquery()
            groups = db.execute(select(failures.c.task_type, failures.c.status, failures.c.error_type,
                                       func.count()).group_by(failures.c.task_type, failures.c.status,
                                                             failures.c.error_type)).all()
            samples = {}
            for kind, status, code, _ in groups:
                if status != TaskStatus.DEAD:
                    continue
                samples[code or "unknown"] = [dict(row) for row in db.execute(failed.where(
                    CrawlTask.status == status, CrawlAttempt.error_type == code).limit(2)).mappings()]
            products = db.execute(select(func.count(), func.sum(case((Product.last_success_at.is_not(None), 1), else_=0)),
                                         func.max(Product.last_success_at)).where(
                    Product.platform == config.platform, Product.region == config.region)).one()
            print(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(), "read_only": True,
                "active_database_queries": [dict(row) for row in processes],
                "market_task_counts": [{"task_type": kind.value, "status": status.value, "count": count}
                                       for kind, status, count in counts],
                "products": products[0], "details_completed": int(products[1] or 0),
                "last_detail_success_at": products[2],
                "failure_groups": [{"task_type": kind.value, "status": status.value, "code": code,
                                    "count": count} for kind, status, code, count in groups],
                "dead_task_samples": samples}, ensure_ascii=False, indent=2, default=str))
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__,
                          "message": "限时只读诊断未完成；未打印连接信息。"}, ensure_ascii=False))
        raise SystemExit(1)
