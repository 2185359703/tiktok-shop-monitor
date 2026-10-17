"""Bounded collection monitoring without rescanning task payloads or building charts."""

from collections import Counter
from datetime import datetime

from sqlalchemy import select

from app.models import CrawlTask, TaskStatus
from app.models.analytics_read import AnalyticsSnapshot
from app.services.analytics_refresh import VERSION, key_for, now, scope
from app.services.collection_rollups import WINDOWS, GROUPS, read_statistics, recent_failures, safe_error


MAX_LEASES = 200
ACTIVE = {"pending", "running", "retry_waiting", "abandoned"}


def error_code(value):
    return safe_error(value)


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


def read_monitor(db, settings, window_minutes=60, *, task_group="source", stamp=None):
    if window_minutes not in WINDOWS or task_group not in GROUPS:
        raise ValueError("invalid_monitor_window_or_group")
    stamp = stamp or now(db)
    statistics = read_statistics(db, settings, window_minutes, task_group, stamp)
    start = datetime.fromisoformat(statistics["window"]["since"])
    until = datetime.fromisoformat(statistics["window"]["until"])
    market = (CrawlTask.platform == settings.platform, CrawlTask.region == settings.region)
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
        **statistics,
        "queue": queue_snapshot(db, settings, stamp),
        "recent_failures": recent_failures(db, settings, start, until, task_group),
        "leases": {"active": len(lease_items) - expired, "expired": expired,
            "truncated": len(lease_rows) > MAX_LEASES, "items": lease_items}}


def monitor_alerts(snapshot, *, scheduler_poll_seconds=5, expected_workers=1, expected_schedulers=1):
    alerts = []
    queue, attempts, runtime = snapshot["queue"], snapshot["attempts"], snapshot["runtime"]
    if queue["status"] == "pending":
        alerts.append({"code": "queue_summary_pending", "severity": "info", "message": "任务汇总尚未发布，队列数量暂不可用。"})
    elif queue["age_seconds"] > 60:
        alerts.append({"code": "queue_summary_stale", "severity": "warning", "message": "任务汇总超过一分钟未更新。"})
    if snapshot["leases"]["expired"]:
        alerts.append({"code": "expired_leases", "severity": "warning", "message": "存在到期租约，等待调度器恢复。"})
    statistics = snapshot.get("statistics", {})
    if statistics.get("status") == "warming":
        alerts.append({"code": "statistics_warming", "severity": "info", "message": "完整监控统计正在准备，尚未发布的数量保持未知。"})
    elif statistics.get("status") == "stale":
        alerts.append({"code": "statistics_stale", "severity": "warning", "message": "监控统计更新延迟，当前显示上次完整发布的数据。"})
    if (attempts["total"] or 0) >= 10 and attempts["success_rate"] < 80:
        alerts.append({"code": "high_failure_rate", "severity": "warning", "message": "当前窗口内尝试成功率低于 80%。"})
    if runtime["status"] != "available":
        alerts.append({"code": "runtime_unknown", "severity": "warning", "message": "运行心跳暂不可用，不能据此判断采集进程离线。"})
    else:
        running = [item for item in runtime["components"] if item["status"] == "online" and item["state"] == "running"]
        online_roles = {item["role"] for item in running}
        if "worker" not in online_roles:
            alerts.append({"code": "worker_heartbeat_missing", "severity": "warning", "message": "暂无近期心跳正常且已就绪的 Worker，采集进程状态待确认。"})
        if "scheduler" not in online_roles:
            alerts.append({"code": "scheduler_heartbeat_missing", "severity": "warning", "message": "暂无近期心跳正常且已就绪的调度器，调度状态待确认。"})
        for role, expected, label in (("worker", expected_workers, "Worker"), ("scheduler", expected_schedulers, "调度器")):
            actual = sum(item["role"] == role for item in running)
            if actual < expected:
                alerts.append({"code": f"{role}_capacity_missing", "severity": "warning",
                    "message": f"{label} 就绪进程 {actual}/{expected}，低于配置的预期数量。"})
        if any(item["state"] != "stopped" and (item["status"] != "online" or item["state"] != "running")
               for item in runtime["components"]):
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
