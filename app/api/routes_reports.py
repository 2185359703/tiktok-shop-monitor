from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.api.routes import require_write_token
from app.models import CrawlTask, TaskStatus, TaskType
from app.reports.summary import build_monitoring_report
from app.reports.exporter import render_html, render_markdown
from app.reports.cache import ReportCache, serialize_report
from app.services.report_service import ensure_report_task
from app.services.snapshot_service import utc_window
from app.services.task_runtime import database_now
from app.storage.database import session_scope


router = APIRouter(prefix="/api/v1/reports", tags=["reports"])
_cache_creation_lock = Lock()


@router.get("/current")
def current_report(request: Request, format: Literal["json", "html", "md"] = "json",
                   lookback_hours: Annotated[int, Query(ge=1, le=720)] = 24):
    # Each App owns its cache, including separately created test applications.
    # The module-level lock only protects initialization, not cached contents.
    with _cache_creation_lock:
        if not hasattr(request.app.state, "report_cache"):
            request.app.state.report_cache = ReportCache()
        cache = request.app.state.report_cache
    settings = request.app.state.settings
    key = settings.platform, settings.region, settings.currency, settings.pricing_context, lookback_hours
    def build():
        with request.app.state.factory() as db:
            report = build_monitoring_report(db, settings, lookback_hours=lookback_hours)
        return serialize_report(report)
    try:
        report = cache.get_or_build(key, build)
    except Exception:
        raise HTTPException(503, "report_generation_unavailable") from None
    types = {"json": "application/json", "html": "text/html", "md": "text/markdown"}
    body = report.bodies[format]
    def chunks():
        for offset in range(0, len(body), 65536):
            yield body[offset:offset + 65536]
    return StreamingResponse(chunks(), media_type=types[format], headers={
        "X-Report-Generated-At": report.generated_at, "Cache-Control": "no-store", "Content-Length": str(len(body))})


class ReportRequest(BaseModel):
    lookback_hours: int = Field(default=24, ge=1, le=720)
    scheduled_for: datetime | None = None

    @field_validator("scheduled_for")
    @classmethod
    def require_timezone(cls, value):
        if value is not None and value.tzinfo is None: raise ValueError("报告窗口需要显式时区。")
        return value.astimezone(timezone.utc) if value is not None else None


@router.post("/jobs", dependencies=[Depends(require_write_token)])
def create_report_job(body: ReportRequest, request: Request):
    settings = request.app.state.settings
    if TaskType.REPORT_GENERATION not in settings.enabled_task_types:
        raise HTTPException(409, "report_handler_not_enabled")
    with session_scope(request.app.state.factory) as db:
        window = body.scheduled_for or utc_window(database_now(db), 3600)
        try:
            task, created = ensure_report_task(db, settings, window, lookback_hours=body.lookback_hours)
        except ValueError:
            raise HTTPException(422, "invalid_report_window") from None
        return {"task_id": task.task_id, "created": created, "status": task.status}


@router.get("/jobs/{task_id}/files/{format}")
def download_report(task_id: str, format: Literal["json", "html", "md"], request: Request):
    settings = request.app.state.settings
    with request.app.state.factory() as db:
        task = db.scalar(select(CrawlTask).where(CrawlTask.task_id == task_id, CrawlTask.task_type == TaskType.REPORT_GENERATION,
                                                CrawlTask.platform == settings.platform, CrawlTask.region == settings.region))
        if task is None: raise HTTPException(404, "report_task_not_found")
        if task.status != TaskStatus.SUCCEEDED: raise HTTPException(409, "report_not_completed")
        value = (task.checkpoint or {}).get("files", {}).get(format)
    if not isinstance(value, str): raise HTTPException(404, "report_file_not_recorded")
    path = Path(value).resolve()
    if not path.is_relative_to(settings.report_directory.resolve()) or not path.is_file() or path.suffix != "." + format:
        raise HTTPException(404, "report_file_not_available")
    types = {"json": "application/json", "html": "text/html", "md": "text/markdown"}
    return FileResponse(path, media_type=types[format], filename=path.name)
