from datetime import timezone

from sqlalchemy import select

from app.models import CrawlTask, ResourceType, TaskType
from app.services.seed_importer import fingerprint
from app.services.task_runtime import database_now


def ensure_report_task(session, settings, window, *, lookback_hours=24):
    if window.tzinfo is None or window > database_now(session) or not 1 <= lookback_hours <= 720:
        raise ValueError("报告窗口需带时区且不在未来，回看区间为 1-720 小时。")
    window = window.astimezone(timezone.utc)
    key = fingerprint({"platform": settings.platform, "region": settings.region, "task_type": "report_generation",
                       "report_type": "clothing_monitor", "window": window.isoformat(), "lookback_hours": lookback_hours,
                       "pricing_context": settings.pricing_context, "currency": settings.currency})
    previous = session.scalar(select(CrawlTask).where(CrawlTask.idempotency_key == key).with_for_update())
    if previous is not None: return previous, False
    task = CrawlTask(platform=settings.platform, region=settings.region, task_type=TaskType.REPORT_GENERATION,
                     resource_type=ResourceType.REPORT, resource_id="clothing_monitor", idempotency_key=key,
                     priority=1, scheduled_for=window, available_at=database_now(session),
                     payload={"schema_version": 1, "report_type": "clothing_monitor", "lookback_hours": lookback_hours,
                              "pricing_context": settings.pricing_context, "currency": settings.currency})
    session.add(task)
    session.flush()
    return task, True
