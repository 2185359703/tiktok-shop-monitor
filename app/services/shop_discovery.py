"""Confirm unique source-backed identities, without guessing missing shops."""

import unicodedata
from dataclasses import replace
from datetime import datetime, timezone

from sqlalchemy import case, func, select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.orm import Session

from app.crawlers.public_shop import PublicPageSnapshot, ShopCandidate, public_url, shop_id_from_url
from app.models import (
    CrawlAttempt, CrawlTask, ResolutionStatus, ResourceType, SeedShopObservation,
    SeedType, Shop, ShopSeedLink, TaskStatus, TaskType,
)
from app.services.seed_importer import fingerprint, seed_task_key
from app.services.shop_profile import ensure_shop_profile_task, shop_profile_window


def normalized_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def matching_candidates(seed: SeedShopObservation, snapshot: PublicPageSnapshot) -> list[ShopCandidate]:
    candidates = [item for item in snapshot.candidates if item.region == seed.region]
    if seed.seed_type == SeedType.SHOP_ID:
        return [item for item in candidates if item.platform_shop_id == seed.seed_value]
    if seed.seed_type == SeedType.SHOP_URL:
        identifier = shop_id_from_url(seed.seed_value)
        return [item for item in candidates if item.platform_shop_id == identifier]
    name = normalized_name(seed.seed_value)
    return [item for item in candidates if any(normalized_name(alias) == name for alias in item.observed_names)]


def product_discovery_key(shop: Shop) -> str:
    return fingerprint({
        "platform": shop.platform, "region": shop.region,
        "platform_shop_id": shop.platform_shop_id, "task_type": "product_discovery",
        "context": "initial_product_discovery",
    })


def ensure_product_discovery_task(session: Session, shop: Shop, source_url: str, now: datetime) -> bool:
    key = product_discovery_key(shop)
    if session.scalar(select(CrawlTask.id).where(CrawlTask.idempotency_key == key).with_for_update()):
        return False
    session.add(CrawlTask(
        platform=shop.platform, region=shop.region, idempotency_key=key,
        task_type=TaskType.PRODUCT_DISCOVERY, resource_type=ResourceType.SHOP,
        resource_id=str(shop.id), priority=4, scheduled_for=now, available_at=now,
        payload={"schema_version": 1, "shop_id": str(shop.id),
                 "platform_shop_id": shop.platform_shop_id,
                 "shop_url": shop.canonical_url, "source_url": source_url},
    ))
    session.flush()
    return True


def finish_identity_task(session, task, snapshot, evidence_path, *, success, error=None):
    now = datetime.now(timezone.utc)
    began = max(snapshot.started_at, task.scheduled_for, task.created_at)
    task.attempt_count += 1
    task.started_at = began
    task.finished_at = now
    task.worker_id = "public-source-bootstrap"
    task.status = TaskStatus.SUCCEEDED if success else TaskStatus.DEAD
    task.last_error = error
    task.checkpoint = {"source_url": snapshot.final_url, "response_hash": snapshot.response_hash,
                       "evidence_path": evidence_path, "outcome": "resolved" if success else "manual_review"}
    attempt = CrawlAttempt(
        task_id=task.id, attempt_no=task.attempt_count, worker_id=task.worker_id,
        request_url=snapshot.final_url, response_status=snapshot.status,
        response_bytes=snapshot.response_bytes, response_hash=snapshot.response_hash,
        latency_ms=snapshot.latency_ms, evidence_path=evidence_path,
        started_at=began, finished_at=now, succeeded=success,
        error_type=None if success else "ambiguous_identity", error_message=error,
    )
    session.add(attempt)
    session.flush()
    from app.services.collection_rollups import record_completed_attempt
    record_completed_attempt(session, task, attempt)


def resolve_seed_from_snapshot(
    session: Session, seed_id: int, snapshot: PublicPageSnapshot, evidence_path: str,
    *, verified_store_entry: bool = False,
    runtime_lease_token: str | None = None,
    runtime_task_id: str | None = None,
) -> tuple[str, bool]:
    """No HTTP inside the transaction. Only pending initial tasks may be completed."""
    seed = session.scalar(select(SeedShopObservation).where(
        SeedShopObservation.id == seed_id
    ).with_for_update())
    if seed is None or seed.resolution_status != ResolutionStatus.PENDING:
        return "already_processed", False
    if seed.platform != "tiktok_shop" or seed.region != "US":
        return "unsupported_market", False
    if verified_store_entry:
        identifier = shop_id_from_url(snapshot.final_url)
        if identifier is None:
            return "source_mismatch", False
        snapshot = replace(snapshot, candidates=tuple(
            item for item in snapshot.candidates if item.platform_shop_id == identifier
        ))
    elif public_url(seed.source_url) != public_url(snapshot.source_url):
        return "source_mismatch", False
    task = session.scalar(select(CrawlTask).where(
        CrawlTask.task_id == runtime_task_id if runtime_task_id is not None else CrawlTask.idempotency_key == seed_task_key(seed.row_hash)
    ).with_for_update())
    managed = runtime_lease_token is not None
    if task is None or task.platform != seed.platform or task.region != seed.region or (
        task.task_type != TaskType.SHOP_DISCOVERY or task.resource_type != ResourceType.SEED or task.resource_id != str(seed.id)
    ) or (runtime_task_id is not None and (not managed or task.payload.get("seed_row_hash") != seed.row_hash)) or (managed and (
        task.status != TaskStatus.RUNNING or task.lease_token != runtime_lease_token
    )) or (not managed and task.status != TaskStatus.PENDING):
        return "task_not_pending", False
    if not managed and task.attempt_count >= task.max_attempts:
        return "attempts_exhausted", False
    matches = matching_candidates(seed, snapshot)
    if not matches:
        # A recommendation/category page is not an exhaustive directory.
        # Keep both observation and task pending for another discovery entrance.
        task.last_error = "来源页本次未覆盖该种子；需补充公开店铺入口，不代表店铺不存在。"
        task.checkpoint = {"outcome": "not_in_source_snapshot", "response_hash": snapshot.response_hash,
                           "source_url": snapshot.final_url, "evidence_path": evidence_path}
        return "not_in_source", False
    link = session.scalar(select(ShopSeedLink).where(ShopSeedLink.seed_id == seed.id).with_for_update())
    linked_shop = session.get(Shop, link.shop_id) if link else None
    conflict = linked_shop and len(matches) == 1 and linked_shop.platform_shop_id != matches[0].platform_shop_id
    if len(matches) != 1 or conflict:
        error = "公开来源匹配到多个店铺ID或与已有映射冲突，需人工确认。"
        seed.resolution_status = ResolutionStatus.AMBIGUOUS
        seed.resolution_error = error
        if not managed:
            finish_identity_task(session, task, snapshot, evidence_path, success=False, error=error)
        return "ambiguous", False
    candidate = matches[0]
    statement = insert(Shop).values(
        platform=seed.platform, region=seed.region,
        platform_shop_id=candidate.platform_shop_id, canonical_name=candidate.canonical_name,
        canonical_url=candidate.canonical_url, discovery_category=seed.category,
        first_seen_at=snapshot.captured_at, last_seen_at=snapshot.captured_at,
    )
    recent = Shop.last_seen_at <= statement.inserted.last_seen_at
    session.execute(statement.on_duplicate_key_update(
        canonical_name=case((recent, statement.inserted.canonical_name), else_=Shop.canonical_name),
        canonical_url=case((recent, func.coalesce(statement.inserted.canonical_url, Shop.canonical_url)),
                           else_=Shop.canonical_url),
        discovery_category=func.coalesce(Shop.discovery_category, statement.inserted.discovery_category),
        last_seen_at=func.greatest(Shop.last_seen_at, statement.inserted.last_seen_at),
    ))
    shop = session.scalar(select(Shop).where(
        Shop.platform == seed.platform, Shop.region == seed.region,
        Shop.platform_shop_id == candidate.platform_shop_id,
    ).with_for_update().execution_options(populate_existing=True))
    if link is None:
        session.add(ShopSeedLink(
            seed_id=seed.id, shop_id=shop.id, platform=seed.platform, region=seed.region,
            resolved_at=snapshot.captured_at, resolution_method="public_ssr_unique_match",
            evidence_url=snapshot.final_url,
        ))
    seed.resolution_status = ResolutionStatus.RESOLVED
    seed.resolution_error = None
    if not managed:
        finish_identity_task(session, task, snapshot, evidence_path, success=True)
    created = ensure_product_discovery_task(session, shop, snapshot.final_url, datetime.now(timezone.utc))
    child_keys = [product_discovery_key(shop)]
    if shop.canonical_url and shop_id_from_url(shop.canonical_url) == shop.platform_shop_id:
        from app.services.shop_profile import shop_profile_task_key
        window = shop_profile_window(datetime.now(timezone.utc), 86400)
        ensure_shop_profile_task(session, shop, window)
        child_keys.append(shop_profile_task_key(shop, window))
    session.flush()
    if (task.payload or {}).get("collection_tracked"):
        from app.services.collection_intake import inherit_launch_tasks
        children = session.scalars(select(CrawlTask).where(CrawlTask.idempotency_key.in_(child_keys)).order_by(CrawlTask.id)
            .with_for_update()).all()
        inherit_launch_tasks(session, task, children)
    return "resolved", created
