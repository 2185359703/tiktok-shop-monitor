"""Bounded collection monitoring without rescanning task payloads or building charts."""

import math
import re
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import CrawlAttempt, CrawlTask, TaskStatus
from app.models.analytics_read import AnalyticsSnapshot
from app.services.analytics_refresh import VERSION, key_for, now, scope


WINDOWS = (15, 60, 360, 1440)
MAX_ATTEMPTS = 20_000
MAX_LEASES = 200
ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
ACTIVE = {"pending", "running", "retry_waiting", "abandoned"}


def error_code(value):
    return value if isinstance(value, str) and ERROR_CODE.fullmatch(value) else "unknown"


def iso(value):
    return value.isoformat() if value is not None else None


def queue_snapshot(db, settings, stamp):
    row = db.get(AnalyticsSnapshot, (VERSION, key_for("task_counts", scope(settings))))
    if row is None:
        return {"status": "pending", "snapshot_at": None, "age_seconds": None,
                "total": None, "active": None, "failed": None, "by_status": {}, "by_type": []}
    counts = row.payload.get("tasks", [])
    by_status = Counter()
    for item in counts:
        by_status[item["status"]] += item["count"]
    return {"status": "fresh" if stamp < row.fresh_until else "stale", "snapshot_at": iso(row.snapshot_at),
            "age_seconds": round(max(0, (stamp - row.snapshot_at).total_seconds()), 1),
            "total": sum(by_status.values()), "active": sum(by_status[key] for key in ACTIVE),
            "failed": by_status["dead"], "by_status": dict(by_status), "by_type": counts}


def read_monitor(db, settings, window_minutes=60, *, stamp=None):
    if window_minutes not in WINDOWS:
        raise ValueError("invalid_monitor_window")
    stamp = stamp or now(db)
    start = stamp - timedelta(minutes=window_minutes)
    bucket_seconds = {15: 60, 60: 300, 360: 1800, 1440: 7200}[window_minutes]
    market = (CrawlTask.platform == settings.platform, CrawlTask.region == settings.region)
    # Only scalar attempt fields are loaded. The newest bounded sample is filtered
    # by market before LIMIT so another tenant cannot crowd it out.
    rows = db.execute(select(CrawlTask.task_id, CrawlTask.task_type,
        CrawlAttempt.attempt_no, CrawlAttempt.worker_id, CrawlAttempt.started_at, CrawlAttempt.finished_at,
        CrawlAttempt.succeeded, CrawlAttempt.error_type, CrawlAttempt.response_status)
        .select_from(CrawlAttempt).join(CrawlTask, CrawlTask.id == CrawlAttempt.task_id)
        .where(*market, CrawlAttempt.finished_at >= start, CrawlAttempt.finished_at <= stamp,
               CrawlAttempt.succeeded.is_not(None))
        .with_hint(CrawlAttempt, "FORCE INDEX (ix_crawl_attempts_monitor)", dialect_name="mysql")
        .prefix_with("STRAIGHT_JOIN", dialect="mysql")
        .order_by(CrawlAttempt.finished_at.desc(), CrawlAttempt.id.desc()).limit(MAX_ATTEMPTS + 1)).all()
    truncated = len(rows) > MAX_ATTEMPTS
    rows = rows[:MAX_ATTEMPTS]
    # Buckets partition this precise selected window, including empty intervals.
    bucket_count = math.ceil(window_minutes * 60 / bucket_seconds)
    trend = [{"time": iso(start + timedelta(seconds=index * bucket_seconds)),
              "completed": 0, "succeeded": 0, "failed": 0} for index in range(bucket_count)]
    failures, durations, errors = [], [], Counter()
    succeeded = 0
    for row in rows:
        success = row.succeeded is True
        succeeded += success
        duration = max(0, (row.finished_at - row.started_at).total_seconds() * 1000)
        durations.append(duration)
        index = min(bucket_count - 1, int((row.finished_at - start).total_seconds() // bucket_seconds))
        trend[index]["completed"] += 1
        trend[index]["succeeded" if success else "failed"] += 1
        if not success:
            code = error_code(row.error_type)
            errors[code] += 1
            if len(failures) < 12:
                failures.append({"task_id": row.task_id, "task_type": str(row.task_type),
                    "attempt_no": row.attempt_no, "worker_id": row.worker_id,
                    "error_type": code, "response_status": row.response_status,
                    "finished_at": iso(row.finished_at)})
    durations.sort()
    # At most 200 live/expired leases; an overflow is explicitly a lower bound.
    lease_rows = db.execute(select(CrawlTask.task_id, CrawlTask.task_type, CrawlTask.worker_id,
        CrawlTask.started_at, CrawlTask.lease_expires_at).where(*market, CrawlTask.status == TaskStatus.RUNNING)
        .with_hint(CrawlTask, "FORCE INDEX (ix_crawl_tasks_lease)", dialect_name="mysql")
        .order_by(CrawlTask.lease_expires_at, CrawlTask.id).limit(MAX_LEASES + 1)).all()
    lease_items = [{"task_id": row.task_id, "task_type": str(row.task_type), "worker_id": row.worker_id,
        "started_at": iso(row.started_at), "lease_expires_at": iso(row.lease_expires_at),
        "expired": row.lease_expires_at is None or row.lease_expires_at <= stamp} for row in lease_rows[:MAX_LEASES]]
    expired = sum(item["expired"] for item in lease_items)
    return {"generated_at": iso(stamp), "scope": {"platform": settings.platform, "region": settings.region},
        "window": {"minutes": window_minutes, "since": iso(start), "until": iso(stamp), "bucket_seconds": bucket_seconds},
        "queue": queue_snapshot(db, settings, stamp),
        "attempts": {"total": len(rows), "succeeded": succeeded, "failed": len(rows) - succeeded,
            "success_rate": round(100 * succeeded / len(rows), 2) if rows else None,
            "avg_duration_ms": round(sum(durations) / len(durations), 1) if durations else None,
            "p95_duration_ms": round(durations[math.ceil(len(durations) * .95) - 1], 1) if durations else None,
            "throughput_per_minute": round(len(rows) / window_minutes, 2),
            "sampled": truncated, "truncated": truncated, "limit": MAX_ATTEMPTS},
        "trend": trend, "errors": [{"error_type": code, "count": count} for code, count in errors.most_common(12)],
        "recent_failures": failures, "leases": {"active": len(lease_items) - expired, "expired": expired,
            "truncated": len(lease_rows) > MAX_LEASES, "items": lease_items}}


def monitor_alerts(snapshot, *, scheduler_poll_seconds=5):
    alerts = []
    queue, attempts, runtime = snapshot["queue"], snapshot["attempts"], snapshot["runtime"]
    if queue["status"] == "pending":
        alerts.append({"code": "queue_summary_pending", "severity": "info", "message": "任务汇总尚未发布，队列数量暂不可用。"})
    elif queue["age_seconds"] > 60:
        alerts.append({"code": "queue_summary_stale", "severity": "warning", "message": "任务汇总超过一分钟未更新。"})
    if snapshot["leases"]["expired"]:
        alerts.append({"code": "expired_leases", "severity": "warning", "message": "存在到期租约，等待调度器恢复。"})
    if attempts["truncated"]:
        alerts.append({"code": "attempt_sample_limit", "severity": "info", "message": "当前窗口超过采样上限，仅统计最近两万次完成尝试。"})
    if attempts["total"] >= 10 and attempts["success_rate"] < 80:
        alerts.append({"code": "high_failure_rate", "severity": "warning", "message": "当前窗口内尝试成功率低于 80%。"})
    if runtime["status"] != "available":
        alerts.append({"code": "runtime_unknown", "severity": "warning", "message": "运行心跳暂不可用，不能据此判断采集进程离线。"})
    else:
        online_roles = {item["role"] for item in runtime["components"] if item["status"] == "online"}
        if "worker" not in online_roles:
            alerts.append({"code": "worker_heartbeat_missing", "severity": "warning", "message": "尚未收到近期 Worker 心跳，采集进程状态待确认。"})
        if "scheduler" not in online_roles:
            alerts.append({"code": "scheduler_heartbeat_missing", "severity": "warning", "message": "尚未收到近期调度器心跳，调度状态待确认。"})
        if any(item["status"] != "online" or item["state"] in {"reconnecting", "degraded"} for item in runtime["components"]):
            alerts.append({"code": "runtime_attention", "severity": "warning", "message": "部分已登记进程心跳过期或正在重连。"})
        def timestamp(value):
            try:
                parsed = datetime.fromisoformat(value)
                return parsed if parsed.tzinfo is not None else None
            except (ValueError, TypeError):
                return None
        observed_at = timestamp(runtime.get("observed_at")) or timestamp(snapshot.get("generated_at"))
        threshold = max(120, scheduler_poll_seconds * 3)
        tick_unknown = tick_stale = False
        for item in runtime["components"]:
            if item["role"] != "scheduler" or item["status"] != "online" or item["state"] == "stopped":
                continue
            succeeded_at = timestamp(item.get("last_success_at"))
            if observed_at is None or succeeded_at is None or (succeeded_at - observed_at).total_seconds() > 30:
                tick_unknown = True
            elif (observed_at - succeeded_at).total_seconds() > threshold:
                tick_stale = True
        if tick_stale:
            alerts.append({"code": "scheduler_tick_stale", "severity": "warning", "message": "调度器仍在上报心跳，但长时间未成功完成调度，请检查运行状态。"})
        if tick_unknown:
            alerts.append({"code": "scheduler_tick_unknown", "severity": "info", "message": "调度器已上报心跳，尚未观察到有效的调度成功时间。"})
    if runtime["source_cooldown_seconds"]:
        alerts.append({"code": "source_cooldown", "severity": "info", "message": "来源站点共享冷却中，网络采集会在冷却结束后继续。"})
    return alerts
