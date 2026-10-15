"""Window-idempotent shop profile tasks and atomic, freshness-aware storage."""

import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import String, cast, exists, or_, select

from app.crawlers.product_images import valid_image_url
from app.crawlers.public_shop import PublicPageError, public_url, shop_id_from_url
from app.crawlers.shop_profile import MAX_UNSIGNED_BIGINT, PARSER_VERSION, PROFILE_VALUE_FIELDS, ParsedShopProfile, ShopProfilePageSnapshot
from app.models import CrawlTask, ResourceType, Shop, ShopProfileSnapshot, TaskType
from app.queue.retry_policy import Failure, TaskFailure
from app.services.seed_importer import fingerprint
from app.services.task_runtime import database_now


COUNT_FIELDS = ("follower_count", "sales_count", "global_sales_count", "video_count", "product_count", "review_count")
BADGE_LIMITS = {"verification_type": 32, "identity_label_key": 64, "identity_label_text": 255}


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("店铺档案时间必须包含时区。")
    return value.astimezone(timezone.utc)


def shop_profile_window(value: datetime, interval_seconds: int) -> datetime:
    if type(interval_seconds) is not int or interval_seconds <= 0:
        raise ValueError("店铺档案采集间隔必须是正整数。")
    whole = int(_utc(value).timestamp())
    return datetime.fromtimestamp(whole - whole % interval_seconds, timezone.utc)


def shop_profile_task_key(shop, scheduled_for: datetime) -> str:
    return fingerprint({"platform": shop.platform, "region": shop.region, "shop_id": str(shop.id),
                        "task_type": "shop_profile", "scheduled_for": _utc(scheduled_for).isoformat()})


def _shop_url(shop) -> str:
    if (shop.platform != "tiktok_shop" or shop.region != "US" or type(shop.id) is not int or shop.id <= 0
            or not isinstance(shop.platform_shop_id, str) or not re.fullmatch(r"[0-9]{1,128}", shop.platform_shop_id)
            or not isinstance(shop.canonical_url, str)
            or shop_id_from_url(shop.canonical_url) != shop.platform_shop_id):
        raise ValueError("店铺档案采集仅接受已确认身份且有匹配公开入口的美国站店铺。")
    return public_url(shop.canonical_url)


def ensure_shop_profile_task(session, shop, scheduled_for, *, available_at=None) -> bool:
    """Create once per shop/window; the caller commits the containing transaction."""
    window = _utc(scheduled_for)
    # Serialize with other task creators and profile writers for this shop.
    row = session.scalar(select(Shop).where(Shop.id == shop.id, Shop.platform == shop.platform,
        Shop.region == shop.region).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise ValueError("店铺尚未持久化，不能创建档案任务。")
    source_url = _shop_url(row)
    key = shop_profile_task_key(row, window)
    previous = session.scalar(select(CrawlTask.id).where(or_(CrawlTask.idempotency_key == key,
        (CrawlTask.platform == row.platform) & (CrawlTask.region == row.region)
        & (CrawlTask.task_type == TaskType.SHOP_PROFILE) & (CrawlTask.resource_type == ResourceType.SHOP)
        & (CrawlTask.resource_id == str(row.id)) & (CrawlTask.scheduled_for == window))).limit(1))
    if previous is not None:
        return False
    ready = _utc(available_at) if available_at is not None else max(window, database_now(session))
    session.add(CrawlTask(
        platform=row.platform, region=row.region, task_type=TaskType.SHOP_PROFILE,
        resource_type=ResourceType.SHOP, resource_id=str(row.id), idempotency_key=key, priority=3,
        scheduled_for=window, available_at=max(window, ready),
        payload={"schema_version": 1, "shop_id": str(row.id), "platform_shop_id": row.platform_shop_id,
                 "shop_url": source_url, "platform": row.platform, "region": row.region,
                 "scheduled_for": window.isoformat()},
    ))
    session.flush()
    return True


def _invalid() -> TaskFailure:
    return TaskFailure(Failure("shop_profile_scope_conflict", "店铺档案、任务、来源身份或观察证据不一致，未写入档案。"))


def _validate_observation(task, shop, snapshot) -> None:
    """Protect the write boundary even when called outside the HTTP handler."""
    try:
        if (not isinstance(snapshot, ShopProfilePageSnapshot) or not isinstance(snapshot.profile, ParsedShopProfile)
                or task.task_type != TaskType.SHOP_PROFILE or task.resource_type != ResourceType.SHOP
                or shop is None or task.platform != shop.platform or task.region != shop.region
                or task.resource_id != str(shop.id) or snapshot.status != 200):
            raise ValueError
        _shop_url(shop)
        profile, payload = snapshot.profile, task.payload
        if (profile.platform_shop_id != shop.platform_shop_id or profile.region != shop.region
                or not isinstance(profile.canonical_name, str) or not 1 <= len(profile.canonical_name.strip()) <= 255
                or re.search(r"[\x00-\x1f\x7f]", profile.canonical_name)
                or not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
                or payload.get("shop_id") != str(shop.id) or payload.get("platform_shop_id") != shop.platform_shop_id
                or payload.get("platform", shop.platform) != shop.platform or payload.get("region", shop.region) != shop.region
                or profile.observation_status not in {"complete", "partial"}):
            raise ValueError
        for source in (payload.get("shop_url"), snapshot.source_url, snapshot.final_url, profile.canonical_url):
            if not isinstance(source, str) or shop_id_from_url(source) != shop.platform_shop_id:
                raise ValueError
        if (not isinstance(snapshot.response_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", snapshot.response_hash)
                or _utc(snapshot.captured_at) < _utc(snapshot.started_at)
                or _utc(snapshot.captured_at) < _utc(task.scheduled_for)):
            raise ValueError
        if "scheduled_for" in payload and _utc(datetime.fromisoformat(payload["scheduled_for"])) != _utc(task.scheduled_for):
            raise ValueError
        _utc(task.scheduled_for)
        if not isinstance(profile.fields, dict) or set(profile.fields) != set(PROFILE_VALUE_FIELDS):
            raise ValueError
        for name in COUNT_FIELDS:
            value = profile.fields[name]
            if value is not None and (type(value) is not int or not 0 <= value <= MAX_UNSIGNED_BIGINT):
                raise ValueError
        for name in (*BADGE_LIMITS, *(name for name in PROFILE_VALUE_FIELDS if name.endswith("_text"))):
            value = profile.fields[name]
            if value is not None and (not isinstance(value, str) or not value.strip()
                    or len(value) > BADGE_LIMITS.get(name, 128) or re.search(r"[\x00-\x1f\x7f]", value)):
                raise ValueError
        rating = profile.fields["rating"]
        if rating is not None and (not isinstance(rating, Decimal) or not rating.is_finite()
                or not 0 <= rating <= 5 or rating != rating.quantize(Decimal("0.01"))):
            raise ValueError
        if profile.fields["logo_url"] is not None and valid_image_url(profile.fields["logo_url"]) is None:
            raise ValueError
        states = profile.field_state
        expected_states = set(PROFILE_VALUE_FIELDS) - {name for name in PROFILE_VALUE_FIELDS if name.endswith("_text")}
        expected_states.add("identity_label_text")
        if not isinstance(states, dict) or set(states) != expected_states:
            raise ValueError
        for value in states.values():
            if (not isinstance(value, dict) or value.get("state") not in {
                    "exact", "display_only", "not_provided", "invalid", "observed", "not_displayed"}
                    or not isinstance(value.get("source"), str)):
                raise ValueError
        json.dumps(states, allow_nan=False)
    except (TypeError, ValueError, PublicPageError, ArithmeticError):
        raise _invalid() from None


def persist_shop_profile(session, task, snapshot, evidence_path) -> dict:
    """Insert immutable history and update only fresher current values, without commit."""
    try:
        identifier = int(task.resource_id)
        if not 0 < identifier <= MAX_UNSIGNED_BIGINT or str(identifier) != task.resource_id:
            raise ValueError
    except (TypeError, ValueError):
        raise _invalid() from None
    shop = session.scalar(select(Shop).where(Shop.id == identifier, Shop.platform == task.platform,
        Shop.region == task.region).with_for_update().execution_options(populate_existing=True))
    _validate_observation(task, shop, snapshot)
    previous = session.scalar(select(ShopProfileSnapshot).where(or_(ShopProfileSnapshot.task_id == task.id,
        (ShopProfileSnapshot.platform == shop.platform) & (ShopProfileSnapshot.region == shop.region)
        & (ShopProfileSnapshot.shop_id == shop.id) & (ShopProfileSnapshot.scheduled_for == task.scheduled_for)))
        .with_for_update())
    if previous is not None:
        if (previous.shop_id != shop.id or previous.platform != task.platform or previous.region != task.region
                or previous.scheduled_for != task.scheduled_for):
            raise _invalid()
        return {"snapshot_id": str(previous.id), "snapshots_written": 0, "window_already_saved": True,
                "profile_updated": False, "product_count_updated": False,
                "observation_status": previous.observation_status}
    profile, captured = snapshot.profile, _utc(snapshot.captured_at)
    values = deepcopy(profile.fields)
    evidence_path = str(evidence_path) if evidence_path is not None else None
    row = ShopProfileSnapshot(
        shop_id=shop.id, platform=shop.platform, region=shop.region, task_id=task.id,
        platform_shop_id=shop.platform_shop_id, canonical_name=profile.canonical_name,
        canonical_url=public_url(profile.canonical_url), scheduled_for=_utc(task.scheduled_for), captured_at=captured,
        source_url=public_url(snapshot.final_url), response_hash=snapshot.response_hash, parser_version=PARSER_VERSION,
        evidence_path=evidence_path, field_state=deepcopy(profile.field_state),
        observation_status=profile.observation_status, **values,
    )
    session.add(row)
    profile_updated = shop.profile_captured_at is None or captured > shop.profile_captured_at
    # Legacy rows have no count timestamp. Conservatively preserve a count that
    # may have been observed with a more recent product discovery.
    count_time = shop.product_count_captured_at
    if count_time is None and shop.product_count is not None:
        count_time = shop.last_seen_at
    count_updated = count_time is None or captured > count_time
    current_states = deepcopy(profile.field_state if profile_updated else (shop.profile_field_state or {}))
    if count_updated:
        current_states["product_count"] = {**deepcopy(profile.field_state["product_count"]),
                                           "captured_at": captured.isoformat()}
    elif profile_updated:
        previous_state = (shop.profile_field_state or {}).get("product_count")
        # A newer product discovery may own this one current field. Keep its
        # provenance as well as its value; the immutable snapshot keeps exactly
        # what this profile response observed.
        current_states["product_count"] = deepcopy(previous_state) if isinstance(previous_state, dict) else {
            "state": "exact" if shop.product_count is not None else "not_provided",
            "source": "shops.product_count", "scope": "platform_on_sell_product_count",
            "display_state": "observed" if shop.product_count_text is not None else "not_provided",
        }
        if count_time is not None:
            current_states["product_count"]["captured_at"] = count_time.isoformat()
    if profile_updated:
        for name, value in values.items():
            if name not in {"product_count", "product_count_text"}:
                setattr(shop, name, value)
        shop.profile_captured_at, shop.profile_source_url = captured, row.source_url
        shop.profile_parser_version, shop.profile_response_hash = PARSER_VERSION, snapshot.response_hash
        shop.profile_evidence_path, shop.profile_field_state = evidence_path, current_states
        shop.profile_observation_status = profile.observation_status
    if count_updated:
        shop.product_count, shop.product_count_text = values["product_count"], values["product_count_text"]
        shop.product_count_captured_at = captured
        if not profile_updated:
            shop.profile_field_state = current_states
    if captured >= shop.last_seen_at:
        shop.canonical_name, shop.canonical_url = profile.canonical_name, row.canonical_url
        shop.last_seen_at = captured
    session.flush()
    from app.services.analytics_refresh import mark_market
    mark_market(session, task.platform, task.region)
    return {"snapshot_id": str(row.id), "snapshots_written": 1, "window_already_saved": False,
            "profile_updated": profile_updated, "stale_observation_skipped": not profile_updated,
            "product_count_updated": count_updated, "observation_status": profile.observation_status}


def enqueue_due_shop_profiles(session, settings, *, limit=100) -> int:
    """Only schedule the current UTC window for the explicit external-ID allowlist."""
    if (not settings.shop_profiles_enabled or TaskType.SHOP_PROFILE not in settings.enabled_task_types
            or not settings.shop_profile_shop_ids or settings.platform != "tiktok_shop" or settings.region != "US"):
        return 0
    if type(limit) is not int or limit < 0:
        raise ValueError("店铺档案调度批量上限必须是非负整数。")
    if limit == 0:
        return 0
    now = database_now(session)
    window = shop_profile_window(now, settings.shop_profile_interval_seconds)
    scheduled = exists(select(CrawlTask.id).where(
        CrawlTask.platform == Shop.platform, CrawlTask.region == Shop.region,
        CrawlTask.task_type == TaskType.SHOP_PROFILE, CrawlTask.resource_type == ResourceType.SHOP,
        CrawlTask.resource_id == cast(Shop.id, String), CrawlTask.scheduled_for == window,
    ))
    created, after = 0, 0
    while created < limit:
        rows = session.scalars(select(Shop).where(
            Shop.platform == settings.platform, Shop.region == settings.region,
            Shop.platform_shop_id.in_(settings.shop_profile_shop_ids), Shop.canonical_url.is_not(None),
            Shop.id > after, ~scheduled,
        ).order_by(Shop.id).limit(limit - created).with_for_update(skip_locked=True)).all()
        if not rows:
            break
        for shop in rows:
            after = shop.id
            try:
                _shop_url(shop)
            except (ValueError, PublicPageError):
                continue
            jitter_max = min(settings.shop_profile_jitter_seconds, settings.shop_profile_interval_seconds - 1)
            jitter = int(shop_profile_task_key(shop, window)[:16], 16) % (jitter_max + 1)
            created += ensure_shop_profile_task(session, shop, window,
                                               available_at=window + timedelta(seconds=jitter))
    return created
