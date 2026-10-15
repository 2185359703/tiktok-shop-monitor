"""Read public collection progress and sanitized failure codes, never credentials."""

import json

from sqlalchemy import select

from app.models import CrawlAttempt, CrawlTask, TaskStatus
from app.queue.redis_gate import RedisGate
from app.services.collection_service import collection_progress
from app.services.seed_importer import read_seed_csv
from app.settings import Settings
from app.storage.database import PROJECT_ROOT, make_engine, make_session_factory


def main():
    config = Settings.load()
    engine, gate = make_engine(config.database_url), RedisGate(config)
    try:
        with make_session_factory(engine)() as db:
            report = collection_progress(db, read_seed_csv(PROJECT_ROOT / "data/seed/shops_us_clothing.csv"), require_baseline=config.baseline_on_detail)
            report.pop("shops")
            rows = db.execute(select(CrawlTask.task_id, CrawlTask.task_type, CrawlTask.status, CrawlAttempt.error_type,
                                     CrawlAttempt.response_status, CrawlAttempt.request_url, CrawlAttempt.evidence_path)
                .join(CrawlAttempt, CrawlAttempt.task_id == CrawlTask.id).where(
                    CrawlTask.status.in_((TaskStatus.DEAD, TaskStatus.RETRY_WAITING)), CrawlAttempt.succeeded.is_(False))
                .order_by(CrawlAttempt.id.desc()).limit(15)).all()
            report["recent_failures"] = [{"task_id": task_id, "task_type": kind.value, "status": status.value,
                "error_type": code, "http_status": response_status, "public_url": url, "evidence_path": evidence}
                for task_id, kind, status, code, response_status, url, evidence in rows]
        report["pause_remaining_seconds"] = round(gate.pause_remaining(), 1)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        gate.close()
        engine.dispose()


if __name__ == "__main__":
    main()
