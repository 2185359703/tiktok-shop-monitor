"""Lease-fenced task lifecycle. Caller owns the surrounding SQL transaction."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CrawlAttempt, CrawlTask, TaskStatus, TaskType
from app.queue.retry_policy import Failure, retry_delay
from app.settings import Settings


class LeaseLost(RuntimeError):
    pass


def database_now(session: Session) -> datetime:
    return session.scalar(select(func.utc_timestamp(6))).replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class TaskLease:
    id: int
    task_id: str
    task_type: TaskType
    resource_id: str
    platform: str
    region: str
    payload: dict[str, Any]
    checkpoint: dict[str, Any] | None
    lease_token: str
    attempt_no: int
    worker_id: str


@dataclass(frozen=True)
class ExecutionResult:
    apply: Callable[[Session, CrawlTask], None] | None = None
    checkpoint: dict[str, Any] | None = None
    request_url: str | None = None
    request_method: str = "GET"
    response_status: int | None = None
    response_bytes: int | None = None
    response_hash: str | None = None
    latency_ms: int | None = None
    evidence_path: str | None = None


def claim_task(session: Session, task_id: str, task_type: TaskType, worker_id: str, settings: Settings) -> TaskLease | None:
    if not worker_id or len(worker_id) > 128:
        raise ValueError("Worker 标识需要 1-128 个字符。")
    task = session.scalar(select(CrawlTask).where(
        CrawlTask.task_id == task_id, CrawlTask.task_type == task_type,
        CrawlTask.platform == settings.platform, CrawlTask.region == settings.region,
    ).with_for_update().execution_options(populate_existing=True))
    now = database_now(session)
    if task is None or task.status not in {TaskStatus.PENDING, TaskStatus.RETRY_WAITING, TaskStatus.ABANDONED}:
        return None
    if task.available_at > now or task.attempt_count >= task.max_attempts or task.lease_token is not None:
        return None
    task.status = TaskStatus.RUNNING
    task.attempt_count += 1
    task.started_at, task.finished_at = now, None
    task.worker_id, task.lease_token = worker_id, str(uuid4())
    task.lease_expires_at = now + timedelta(seconds=settings.lease_seconds)
    session.add(CrawlAttempt(task_id=task.id, attempt_no=task.attempt_count, worker_id=worker_id, started_at=now))
    session.flush()
    return TaskLease(task.id, task.task_id, task.task_type, task.resource_id, task.platform, task.region,
                     dict(task.payload), dict(task.checkpoint) if task.checkpoint else None,
                     task.lease_token, task.attempt_count, worker_id)


def owned_task(session: Session, lease: TaskLease) -> CrawlTask:
    task = session.scalar(select(CrawlTask).where(CrawlTask.id == lease.id).with_for_update().execution_options(populate_existing=True))
    if (task is None or task.status != TaskStatus.RUNNING or task.lease_token != lease.lease_token
            or task.lease_expires_at is None or task.lease_expires_at <= database_now(session)):
        raise LeaseLost("当前 Worker 已失去租约，禁止写入任务结果。")
    return task


def renew_lease(session: Session, lease: TaskLease, settings: Settings) -> None:
    task = owned_task(session, lease)
    task.lease_expires_at = database_now(session) + timedelta(seconds=settings.lease_seconds)


def save_checkpoint(session: Session, lease: TaskLease, checkpoint: dict) -> None:
    task = owned_task(session, lease)
    task.checkpoint = dict(checkpoint)


def finalize_task(session: Session, lease: TaskLease, settings: Settings, *, result: ExecutionResult | None = None,
                  failure: Failure | None = None) -> TaskStatus:
    if (result is None) == (failure is None):
        raise ValueError("结果和失败必须且只能提供一个。")
    task = owned_task(session, lease)
    attempt = session.scalar(select(CrawlAttempt).where(
        CrawlAttempt.task_id == task.id, CrawlAttempt.attempt_no == lease.attempt_no,
    ).with_for_update())
    if attempt is None or attempt.succeeded is not None:
        raise LeaseLost("尝试记录已被其他执行完成。")
    if result is not None:
        if result.apply:
            result.apply(session, task)
        # Domain writes and task completion must share one transaction.
        owned_task(session, lease)
        task.status, task.last_error = TaskStatus.SUCCEEDED, None
        if result.checkpoint is not None:
            task.checkpoint = dict(result.checkpoint)
        for field in ("request_url", "request_method", "response_status", "response_bytes", "response_hash", "latency_ms", "evidence_path"):
            setattr(attempt, field, getattr(result, field))
        attempt.succeeded = True
    else:
        task.last_error = failure.message
        attempt.succeeded, attempt.error_type, attempt.error_message = False, failure.code, failure.message
        attempt.response_status = failure.response_status
        for field in ("request_url", "response_hash", "response_bytes", "evidence_path"):
            setattr(attempt, field, getattr(failure, field))
        if failure.retryable and task.attempt_count < task.max_attempts:
            delay = failure.delay_seconds if failure.delay_seconds is not None else retry_delay(
                task.task_id, task.attempt_count, settings.retry_base_seconds, settings.retry_max_seconds)
            task.status, task.available_at = TaskStatus.RETRY_WAITING, database_now(session) + timedelta(seconds=delay)
        else:
            task.status = TaskStatus.DEAD
    now = database_now(session)
    attempt.finished_at = now
    task.finished_at = now if task.status in {TaskStatus.SUCCEEDED, TaskStatus.DEAD} else None
    task.lease_token = task.lease_expires_at = None
    session.flush()
    return task.status


def recover_expired_leases(session: Session, settings: Settings, *, limit: int = 100) -> int:
    if not settings.enabled_task_types:
        return 0
    now = database_now(session)
    tasks = session.scalars(select(CrawlTask).where(
        CrawlTask.platform == settings.platform, CrawlTask.region == settings.region,
        CrawlTask.task_type.in_(settings.enabled_task_types), CrawlTask.status == TaskStatus.RUNNING,
        CrawlTask.lease_expires_at <= now,
    ).order_by(CrawlTask.id).limit(limit).with_for_update(skip_locked=True)).all()
    for task in tasks:
        attempt = session.scalar(select(CrawlAttempt).where(
            CrawlAttempt.task_id == task.id, CrawlAttempt.attempt_no == task.attempt_count,
        ).with_for_update())
        if attempt is not None and attempt.succeeded is None:
            attempt.succeeded, attempt.finished_at = False, now
            attempt.error_type, attempt.error_message = "lease_expired", "Worker 租约到期，任务由调度器恢复。"
        task.status = TaskStatus.ABANDONED if task.attempt_count < task.max_attempts else TaskStatus.DEAD
        task.last_error = "Worker 租约到期，已保留检查点和历史尝试。"
        task.available_at = now + timedelta(seconds=settings.retry_base_seconds)
        task.finished_at = now if task.status == TaskStatus.DEAD else None
        task.lease_token = task.lease_expires_at = None
    session.flush()
    return len(tasks)


def defer_unclaimed_task(session: Session, task_id: str, settings: Settings, delay: float) -> None:
    task = session.scalar(select(CrawlTask).where(
        CrawlTask.task_id == task_id, CrawlTask.platform == settings.platform, CrawlTask.region == settings.region,
        CrawlTask.task_type.in_(settings.enabled_task_types),
    ).with_for_update())
    if task is not None and task.status in {TaskStatus.PENDING, TaskStatus.RETRY_WAITING, TaskStatus.ABANDONED} and task.lease_token is None:
        task.available_at = max(task.available_at, database_now(session) + timedelta(seconds=max(delay, 1)))
