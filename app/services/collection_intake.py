"""Durable, explicit collection launches. HTTP submits work without source IO."""

from collections import Counter
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.exc import IntegrityError, OperationalError

from app.crawlers.product_list import product_id_from_url
from app.crawlers.public_shop import PublicPageError, public_url, shop_id_from_url
from app.models import CrawlTask, Product, ResourceType, SeedShopObservation, Shop, TaskStatus, TaskType
from app.models.collection_launch import CollectionLaunch, CollectionLaunchTask
from app.services.collection_management import ACTIVE_STATES, ManagementError, audit, is_collection_paused, market
from app.services.collection_resource_guard import CollectionResourceBusy, lock_idle_resources
from app.services.seed_importer import SeedBatch, import_seed_batch, normalize_seed, fingerprint, seed_task_key
from app.services.task_runtime import database_now


LAUNCH_TYPES = (TaskType.SHOP_DISCOVERY, TaskType.PRODUCT_DISCOVERY, TaskType.PRODUCT_DETAIL, TaskType.PRODUCT_REVIEWS)


def attach_tasks(session, launch_ids, tasks, *, role="descendant"):
    """A task may be shared; never downgrade a root membership on replay."""
    tasks = {task.id: task for task in tasks}
    launch_ids = sorted(set(launch_ids))
    if not tasks or not launch_ids:
        return
    rows = [{"launch_id": launch_id, "task_id": task_id, "member_role": role}
            for launch_id in launch_ids for task_id in sorted(tasks)]
    statement = insert(CollectionLaunchTask).values(rows)
    session.execute(statement.on_duplicate_key_update(task_id=statement.inserted.task_id))
    for task in tasks.values():
        if not (task.payload or {}).get("collection_tracked"):
            task.payload = {**(task.payload or {}), "collection_tracked": True}
    session.flush()


def inherit_launch_tasks(session, parent, tasks, *, role="descendant"):
    # Ordinary pre-console tasks have no membership query or new-table dependency.
    if not (parent.payload or {}).get("collection_tracked"):
        return
    launch_ids = session.scalars(select(CollectionLaunchTask.launch_id).where(CollectionLaunchTask.task_id == parent.id)).all()
    attach_tasks(session, launch_ids, tasks, role=role)


def normalize_definition(settings, definition):
    kind = TaskType(definition["task_type"])
    if kind not in LAUNCH_TYPES:
        raise ManagementError("unsupported_launch_type", 422)
    ids, urls = list(definition.get("resource_ids") or []), list(definition.get("shop_urls") or [])
    if kind == TaskType.SHOP_DISCOVERY:
        if ids or not urls or settings.platform != "tiktok_shop" or settings.region != "US":
            raise ManagementError("invalid_launch_scope", 422)
        cleaned = []
        for url in urls:
            try:
                value = public_url(url)
            except (PublicPageError, TypeError, ValueError):
                raise ManagementError("invalid_shop_entry", 422) from None
            if shop_id_from_url(value) is None or len(value) > 255:
                raise ManagementError("invalid_shop_entry", 422)
            cleaned.append(value)
        urls = sorted(set(cleaned))
    elif urls or not ids:
        raise ManagementError("invalid_launch_scope", 422)
    ids = sorted(set(ids), key=int)
    if not 1 <= len(ids or urls) <= 100:
        raise ManagementError("invalid_launch_scope", 422)
    max_pages = definition.get("max_pages")
    if kind in (TaskType.PRODUCT_DISCOVERY, TaskType.PRODUCT_REVIEWS):
        if max_pages is not None and (type(max_pages) is not int or not 1 <= max_pages <= 500):
            raise ManagementError("invalid_launch_budget", 422)
    elif max_pages is not None:
        raise ManagementError("invalid_launch_budget", 422)
    return {"name": definition["name"], "task_type": kind.value, "resource_ids": ids, "shop_urls": urls, "max_pages": max_pages}


def launch_resources(session, settings, kind, identifiers):
    model = Shop if kind == TaskType.PRODUCT_DISCOVERY else Product
    try:
        resources = lock_idle_resources(session, settings, kind, identifiers)
    except CollectionResourceBusy:
        raise ManagementError("collection_resource_busy") from None
    if len(resources) != len(identifiers):
        raise ManagementError("invalid_launch_resources", 422)
    shops = {row.id: row for row in resources} if model is Shop else {
        row.id: row for row in session.scalars(select(Shop).where(*market(Shop, settings),
            Shop.id.in_(sorted({value.shop_id for value in resources})))).all()}
    for resource in resources:
        shop = resource if model is Shop else shops.get(resource.shop_id)
        if shop is None or not shop.platform_shop_id.isascii() or not shop.platform_shop_id.isdecimal() or (
                kind == TaskType.PRODUCT_DISCOVERY and shop_id_from_url(shop.canonical_url or "") != shop.platform_shop_id):
            raise ManagementError("invalid_launch_resources", 422)
        if model is Product and (product_id_from_url(resource.product_url or "") != resource.platform_product_id or (
                kind == TaskType.PRODUCT_DETAIL and (resource.currency != "USD" or resource.pricing_context != "public_listed"))):
            raise ManagementError("invalid_launch_resources", 422)
    return resources, shops


def _root_tasks(session, settings, launch, definition, stamp):
    kind = launch.task_type
    if kind == TaskType.SHOP_DISCOVERY:
        rows = tuple(normalize_seed({"region": settings.region, "category": "manual_entry", "seed_type": "shop_url",
            "seed_value": url, "shop_url": url, "source_url": url, "verification_status": "operator_public_entry",
            "observed_at": stamp.date().isoformat()}, index + 1) for index, url in enumerate(definition["shop_urls"]))
        try:
            result = import_seed_batch(session, SeedBatch(tuple(sorted(rows, key=lambda row: row.row_hash)), len(rows)))
        except OperationalError as error:
            if getattr(error.orig, "args", (None,))[0] == 3572:
                raise ManagementError("collection_resource_busy") from None
            raise
        observations = session.scalars(select(SeedShopObservation).where(
            SeedShopObservation.row_hash.in_([row.row_hash for row in rows])).order_by(SeedShopObservation.id)).all()
        tasks = session.scalars(select(CrawlTask).where(CrawlTask.idempotency_key.in_(
            [seed_task_key(seed.row_hash) for seed in observations])).order_by(CrawlTask.id).with_for_update()).all()
        if len(tasks) != len(rows):
            raise ManagementError("launch_task_unavailable")
        launch.created_task_count = result["new_discovery_tasks"]
        launch.reused_task_count = len(tasks) - launch.created_task_count
        return tasks
    resources, shops = launch_resources(session, settings, kind, definition["resource_ids"])
    tasks = []
    for resource in resources:
        shop = resource if kind == TaskType.PRODUCT_DISCOVERY else shops[resource.shop_id]
        task_id = str(uuid4())
        payload = {"schema_version": 1, "shop_id": str(shop.id), "platform_shop_id": shop.platform_shop_id,
                   "root_task_id": task_id}
        if kind == TaskType.PRODUCT_DISCOVERY:
            payload.update(shop_url=shop.canonical_url, page_number=1, max_pages=launch.max_pages)
        else:
            payload.update(product_id=str(resource.id), platform_product_id=resource.platform_product_id,
                           product_url=resource.product_url, pricing_context="public_listed")
            if kind == TaskType.PRODUCT_REVIEWS:
                from app.crawlers.product_reviews import REVIEW_SOURCE_VERSION
                payload.update(source_version=REVIEW_SOURCE_VERSION, page_number=1, page_size=5,
                               max_pages=launch.max_pages, seen_page_hashes=[])
        task = CrawlTask(task_id=task_id, platform=settings.platform, region=settings.region, task_type=kind,
            resource_type=ResourceType.SHOP if kind == TaskType.PRODUCT_DISCOVERY else ResourceType.PRODUCT,
            resource_id=str(resource.id), idempotency_key=fingerprint({"launch": str(launch.id), "resource": str(resource.id)}),
            payload=payload, priority=4, scheduled_for=stamp, available_at=stamp)
        session.add(task)
        tasks.append(task)
    session.flush()
    launch.created_task_count = len(tasks)
    return tasks


def create_launch(session, settings, definition, request_id):
    definition = normalize_definition(settings, definition)
    key = fingerprint({"platform": settings.platform, "region": settings.region, "launch_request": str(request_id)})
    digest = fingerprint(definition)
    existing = session.scalar(select(CollectionLaunch).where(CollectionLaunch.request_key == key))
    if existing is not None:
        if existing.request_hash != digest:
            raise ManagementError("idempotency_conflict")
        return existing, False
    if is_collection_paused(session, settings, lock=True):
        raise ManagementError("collection_paused")
    if TaskType(definition["task_type"]) not in settings.enabled_task_types:
        raise ManagementError("collector_not_enabled")
    if len(definition["resource_ids"] or definition["shop_urls"]) > min(100, settings.dispatch_batch_size):
        raise ManagementError("invalid_launch_scope", 422)
    kind = TaskType(definition["task_type"])
    effective_pages = definition["max_pages"]
    if kind in (TaskType.PRODUCT_DISCOVERY, TaskType.PRODUCT_REVIEWS):
        limit = settings.product_discovery_max_pages if kind == TaskType.PRODUCT_DISCOVERY else settings.review_max_pages
        effective_pages = limit if effective_pages is None else effective_pages
        if effective_pages > limit:
            raise ManagementError("invalid_launch_budget", 422)
    stamp = database_now(session)
    launch = CollectionLaunch(platform=settings.platform, region=settings.region, request_key=key, request_hash=digest,
        name=definition["name"], task_type=TaskType(definition["task_type"]), requested_count=len(definition["resource_ids"] or definition["shop_urls"]),
        max_pages=effective_pages)
    try:
        with session.begin_nested():
            session.add(launch)
            session.flush()
    except IntegrityError as error:
        if getattr(error.orig, "args", (None,))[0] != 1062:
            raise
        existing = session.scalar(select(CollectionLaunch).where(CollectionLaunch.request_key == key).with_for_update())
        if existing is None or existing.request_hash != digest:
            raise ManagementError("idempotency_conflict") from None
        return existing, False
    tasks = _root_tasks(session, settings, launch, definition, stamp)
    attach_tasks(session, [launch.id], tasks, role="root")
    audit(session, settings, "collection_launched", "launch", launch.id, task_type=launch.task_type.value,
          requested_count=launch.requested_count, created_task_count=launch.created_task_count,
          reused_task_count=launch.reused_task_count, max_pages=launch.max_pages)
    session.flush()
    return launch, True


def launch_summary(session, launch):
    counts = {"root": Counter(), "descendant": Counter(), "retry": Counter()}
    for role, status, count in session.execute(select(CollectionLaunchTask.member_role, CrawlTask.status, func.count())
            .join(CrawlTask, CrawlTask.id == CollectionLaunchTask.task_id).where(CollectionLaunchTask.launch_id == launch.id)
            .group_by(CollectionLaunchTask.member_role, CrawlTask.status)):
        counts[role][status.value] = count
    root_counts = counts["root"]
    descendant_counts = counts["descendant"] + counts["retry"]
    all_counts = root_counts + descendant_counts
    active = sum(all_counts[state.value] for state in ACTIVE_STATES)
    total, succeeded = sum(all_counts.values()), all_counts[TaskStatus.SUCCEEDED.value]
    status = ("running" if all_counts[TaskStatus.RUNNING.value] else "queued") if active else (
        "succeeded" if total and succeeded == total else "partial" if succeeded else "failed")
    if launch.cancel_requested_at is not None:
        status = "cancelled" if launch.cancel_completed_at is not None else "cancelling"
    return {"id": str(launch.id), "name": launch.name, "task_type": launch.task_type,
        "requested_count": launch.requested_count, "created_task_count": launch.created_task_count,
        "reused_task_count": launch.reused_task_count, "max_pages": launch.max_pages, "created_at": launch.created_at,
        "cancel_requested_at": launch.cancel_requested_at, "cancel_completed_at": launch.cancel_completed_at,
        "status": status, "root_status_counts": dict(root_counts), "descendant_status_counts": dict(descendant_counts),
        "root_task_count": sum(root_counts.values()), "descendant_task_count": sum(descendant_counts.values()),
        "coverage_scope": "roots_and_new_descendants"}
