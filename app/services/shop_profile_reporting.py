"""Separate profile observation coverage from product collection progress."""

from datetime import timedelta

from sqlalchemy import func, select

from app.models import CrawlTask, ResourceType, TaskStatus, TaskType
from app.services.task_runtime import database_now


def shop_profile_coverage(session, shops, *, interval_seconds=86400):
    shops = list(shops)
    now = database_now(session)
    ranked = select(CrawlTask.id, func.row_number().over(
        partition_by=CrawlTask.resource_id,
        order_by=(CrawlTask.scheduled_for.desc(), CrawlTask.id.desc()),
    ).label("position")).where(
        CrawlTask.platform == "tiktok_shop", CrawlTask.region == "US",
        CrawlTask.task_type == TaskType.SHOP_PROFILE, CrawlTask.resource_type == ResourceType.SHOP,
        CrawlTask.resource_id.in_([str(shop.id) for shop in shops]),
    ).subquery()
    latest = select(ranked.c.id).where(ranked.c.position == 1)
    statuses = dict(session.execute(select(CrawlTask.resource_id, CrawlTask.status).where(CrawlTask.id.in_(latest))).all())
    active = {TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRY_WAITING, TaskStatus.ABANDONED}
    counts = {"target_shops": len(shops), "profile_observed_shops": 0, "profile_complete_shops": 0,
              "profile_partial_shops": 0, "profile_failed_shops": 0, "profile_pending_shops": 0,
              "profile_stale_shops": 0, "field_coverage": {}}
    fields = ("logo_url", "verification_type", "identity_label_key", "follower_count", "sales_count", "video_count")
    for field in fields:
        counts["field_coverage"][field] = {"observed": 0, "exact": 0, "display_only": 0, "checked": 0}
    for shop in shops:
        status = statuses.get(str(shop.id))
        observed = shop.profile_captured_at is not None
        counts["profile_observed_shops"] += observed
        counts["profile_complete_shops"] += observed and shop.profile_observation_status == "complete"
        counts["profile_partial_shops"] += observed and shop.profile_observation_status == "partial"
        counts["profile_failed_shops"] += status in {TaskStatus.DEAD, TaskStatus.CANCELLED}
        counts["profile_pending_shops"] += status in active or (not observed and status is None)
        counts["profile_stale_shops"] += observed and shop.profile_captured_at <= now - timedelta(seconds=interval_seconds)
        for field in fields:
            state = (shop.profile_field_state or {}).get(field, {}).get("state")
            values = counts["field_coverage"][field]
            values["observed"] += observed and state in {"exact", "display_only", "observed"}
            values["exact"] += observed and state == "exact"
            values["display_only"] += observed and state == "display_only"
            values["checked"] += observed and state in {"exact", "display_only", "observed", "not_displayed"}
    for values in counts["field_coverage"].values():
        values["coverage_ratio"] = values["checked"] / len(shops) if shops else None
        values["exact_ratio"] = values["exact"] / len(shops) if shops else None
    counts["full_coverage"] = bool(shops) and counts["profile_complete_shops"] == len(shops) and not (
        counts["profile_failed_shops"] or counts["profile_pending_shops"] or counts["profile_stale_shops"])
    return counts
