"""Durable image-only work over the existing, fixed clothing seed scope.

This module never requests a website. The normal lease-fenced detail handler
owns HTTP, retries and the shared request gate; this module only plans tasks,
checks their resource identity and persists their public photo projection.
"""

import json
import re
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from sqlalchemy import and_, case, exists, func, select
from sqlalchemy.dialects.mysql import insert

from app.crawlers.product_images import IMAGE_SOURCE_VERSION
from app.crawlers.product_content import CONTENT_SOURCE_VERSION
from app.crawlers.product_list import product_id_from_url
from app.crawlers.public_shop import PublicPageError, public_url
from app.models import (
    CrawlTask, Product, ResolutionStatus, ResourceType, SeedShopObservation,
    Shop, ShopSeedLink, TaskStatus, TaskType,
)
from app.models.product_image import ProductImage
from app.models.product_content import ProductContent
from app.services.product_content import persist_product_content, product_content_table_available
from app.queue.retry_policy import Failure, TaskFailure
from app.services.product_images import images_table_available, persist_product_images
from app.services.seed_importer import fingerprint, read_seed_csv
from app.services.task_runtime import database_now
from app.storage.database import PROJECT_ROOT


IMAGE_MODE = "images_only"
MEDIA_MODE = "media_content_only"
BACKFILL_MODES = (IMAGE_MODE, MEDIA_MODE)
FULL_MODE = "full"
ACTIVE_IMAGE_STATUSES = (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRY_WAITING, TaskStatus.ABANDONED)
FIXED_IMAGE_SEED_FILE = PROJECT_ROOT / "data/seed/shops_us_clothing.csv"


def capture_mode_expression():
    """Historical detail payloads without a mode retain full-detail semantics."""
    return func.coalesce(CrawlTask.payload["capture_mode"].as_string(), FULL_MODE)


def fixed_image_seed_batch():
    """Use the established 100-store CSV, not an ad-hoc list or arbitrary IDs."""
    batch = read_seed_csv(FIXED_IMAGE_SEED_FILE)
    if len(batch.rows) != 100 or any(
        row.normalized["region"] != "US" or not row.normalized["shop_url"]
        for row in batch.rows
    ):
        raise ValueError("图片补采仅支持既有的100条美国站店铺种子名单。")
    return batch


def _failure(code="image_resource_scope_conflict"):
    return TaskFailure(Failure(code, "图片任务、公开响应与已存商品身份不一致，未写入图库。"))


def _load_image_product(session, product_id, payload, platform, region, *, lock=False):
    if (type(product_id) is not int or product_id <= 0 or not isinstance(payload, dict)
            or payload.get("capture_mode") not in BACKFILL_MODES
            or payload.get("image_source_version") != IMAGE_SOURCE_VERSION
            or (payload.get("capture_mode") == MEDIA_MODE and payload.get("content_source_version") != CONTENT_SOURCE_VERSION)
            or payload.get("product_id") != str(product_id)):
        raise _failure("invalid_image_task_payload")
    query = select(Product).where(Product.id == product_id, Product.platform == platform,
                                  Product.region == region).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    product = session.scalar(query)
    shop = session.get(Shop, product.shop_id) if product is not None else None
    if (product is None or shop is None or shop.platform != platform or shop.region != region
            or payload.get("platform_product_id") != product.platform_product_id
            or payload.get("shop_id") != str(product.shop_id)
            or payload.get("platform_shop_id") != shop.platform_shop_id
            or payload.get("pricing_context") != "public_listed"
            or product.pricing_context != "public_listed" or product.currency != "USD"):
        raise _failure()
    try:
        if (product_id_from_url(public_url(payload["product_url"])) != product.platform_product_id
                or product_id_from_url(public_url(product.product_url)) != product.platform_product_id):
            raise _failure()
    except (KeyError, TypeError, ValueError, PublicPageError):
        raise _failure("invalid_image_task_payload") from None
    return product, shop


def _current_image_exists(session, product_id, *, detail_only=False):
    conditions = [ProductImage.product_id == product_id, ProductImage.is_current.is_(True)]
    if detail_only:
        conditions.append(ProductImage.source_kind == "detail")
    return bool(session.scalar(select(exists().where(*conditions))))


def image_capture_state(session, product_id: int, payload: dict, platform: str, region: str) -> bool:
    """Validate a durable task before HTTP; True permits a zero-request skip."""
    _load_image_product(session, product_id, payload, platform, region)
    # A listing cover is useful for display, but does not prove that the
    # requested detail gallery has been collected.
    images = _current_image_exists(session, product_id, detail_only=True)
    if images:
        latest = session.execute(select(CrawlTask.checkpoint["gallery_complete"].as_boolean()).where(
            CrawlTask.platform == platform, CrawlTask.region == region,
            CrawlTask.task_type == TaskType.PRODUCT_DETAIL, CrawlTask.resource_type == ResourceType.PRODUCT,
            CrawlTask.resource_id == str(product_id), CrawlTask.status == TaskStatus.SUCCEEDED
        ).order_by(CrawlTask.finished_at.desc(), CrawlTask.id.desc()).limit(1)).first()
        # Legacy galleries have usable links, but no proof of completeness.
        # Only a completed observation may skip a requested gallery capture.
        images = latest is not None and latest[0] is True
    if payload.get("capture_mode") == MEDIA_MODE:
        return images and bool(session.scalar(select(exists().where(
            ProductContent.product_id == product_id, ProductContent.source_version == CONTENT_SOURCE_VERSION,
            ProductContent.completeness == "complete"))))
    return images


def persist_image_observation(session, task, snapshot) -> dict:
    """Write only images under the same product lock used by normal details."""
    try:
        product_id = int(task.resource_id)
    except (TypeError, ValueError):
        raise _failure("invalid_image_task_payload") from None
    if task.resource_type != ResourceType.PRODUCT or task.task_type != TaskType.PRODUCT_DETAIL:
        raise _failure("invalid_image_task_payload")
    product, shop = _load_image_product(session, product_id, task.payload, task.platform, task.region, lock=True)
    detail = snapshot.detail
    try:
        valid = (snapshot.status == 200 and snapshot.captured_at.tzinfo is not None
                 and detail.platform_product_id == product.platform_product_id
                 and detail.platform_shop_id == shop.platform_shop_id
                 and product_id_from_url(public_url(snapshot.source_url)) == product.platform_product_id
                 and product_id_from_url(public_url(snapshot.final_url)) == product.platform_product_id)
    except (AttributeError, TypeError, ValueError, PublicPageError):
        raise _failure() from None
    if not valid:
        raise _failure()
    saved = persist_product_images(session, product.id, detail.images, source_url=public_url(snapshot.final_url),
                                   source_kind="detail", observed_at=snapshot.captured_at)
    summary = {"capture_mode": task.payload["capture_mode"], "image_source_version": IMAGE_SOURCE_VERSION,
            "outcome": "available" if saved or _current_image_exists(session, product.id) else "no_images",
            "images_upserted": saved,
            "gallery_complete": bool(detail.images) and all(image.source_complete for image in detail.images),
            "observed_at": snapshot.captured_at.isoformat()}
    if task.payload["capture_mode"] == MEDIA_MODE:
        if detail.content is None:
            raise _failure("missing_content_projection")
        summary.update(persist_product_content(session, product.id, detail.content, source_url=snapshot.final_url,
            response_hash=snapshot.response_hash, observed_at=snapshot.captured_at))
    return summary


def save_image_evidence(snapshot, directory, *, capture_mode=IMAGE_MODE) -> Path:
    """Save image-only public evidence, excluding full detail/SKU/body data."""
    if not isinstance(snapshot.response_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", snapshot.response_hash):
        raise ValueError("图片响应摘要无效。")
    detail = snapshot.detail
    source, final = public_url(snapshot.source_url), public_url(snapshot.final_url)
    if product_id_from_url(source) != detail.platform_product_id or product_id_from_url(final) != detail.platform_product_id:
        raise ValueError("图片证据与商品身份不一致。")
    if capture_mode not in BACKFILL_MODES:
        raise ValueError("补采证据模式无效。")
    record = {"evidence_kind": "public_product_image_projection" if capture_mode == IMAGE_MODE else "public_product_media_projection", "capture_mode": capture_mode,
              "image_source_version": IMAGE_SOURCE_VERSION,
              "platform_product_id": detail.platform_product_id, "platform_shop_id": detail.platform_shop_id,
              "source_url": source, "final_url": final,
              "status": snapshot.status, "response_hash": snapshot.response_hash,
              "response_bytes": snapshot.response_bytes, "latency_ms": snapshot.latency_ms,
              "started_at": snapshot.started_at.isoformat(), "captured_at": snapshot.captured_at.isoformat(),
              "images": [asdict(image) for image in detail.images]}
    if capture_mode == MEDIA_MODE:
        record["content"] = asdict(detail.content) if detail.content is not None else None
        record["content_source_version"] = CONTENT_SOURCE_VERSION
        record["content_source"] = detail.content_source
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    suffix = "images" if capture_mode == IMAGE_MODE else "media"
    path = directory / f"{snapshot.response_hash}.{suffix}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _seed_scope(session, settings, batch):
    hashes = sorted(row.row_hash for row in batch.rows)
    if not hashes or any(row.normalized["region"] != settings.region or not row.normalized["shop_url"] for row in batch.rows):
        raise ValueError("图片补采名单必须为当前市场的既有公开店铺。")
    rows = session.execute(select(SeedShopObservation.row_hash, ShopSeedLink.shop_id)
                           .join(ShopSeedLink, and_(ShopSeedLink.seed_id == SeedShopObservation.id,
                                                   ShopSeedLink.platform == SeedShopObservation.platform,
                                                   ShopSeedLink.region == SeedShopObservation.region))
                           .join(Shop, Shop.id == ShopSeedLink.shop_id)
                           .where(SeedShopObservation.row_hash.in_(hashes),
                                  SeedShopObservation.platform == settings.platform,
                                  SeedShopObservation.region == settings.region,
                                  SeedShopObservation.resolution_status == ResolutionStatus.RESOLVED,
                                  Shop.platform == settings.platform, Shop.region == settings.region)).all()
    if {row[0] for row in rows} != set(hashes):
        raise ValueError("部分既有种子尚未确认店铺映射，未创建图片任务。")
    shop_ids = sorted({row[1] for row in rows})
    scope_hash = fingerprint({"platform": settings.platform, "region": settings.region,
                              "seed_row_hashes": hashes, "shop_ids": shop_ids})
    return shop_ids, scope_hash


def image_task_key(product_id, platform_product_id, platform, region):
    """Stable globally across restarts and overlapping seed-scope revisions."""
    return fingerprint({"platform": platform, "region": region, "product_id": str(product_id),
                        "platform_product_id": platform_product_id, "capture_mode": IMAGE_MODE,
                        "image_source_version": IMAGE_SOURCE_VERSION, "context": "historical_missing_images"})


def media_task_key(product_id, platform_product_id, platform, region):
    return fingerprint({"platform": platform, "region": region, "product_id": str(product_id),
                        "platform_product_id": platform_product_id, "capture_mode": MEDIA_MODE,
                        "image_source_version": IMAGE_SOURCE_VERSION, "content_source_version": CONTENT_SOURCE_VERSION})


def enqueue_image_tasks(session, settings, batch, *, execute=False, workers_ready=False, after_id=0, limit=500,
                        capture_mode=IMAGE_MODE) -> dict:
    """Plan/create one bounded page; caller owns its transaction and commits.

    Production entry points obtain ``batch`` from ``fixed_image_seed_batch``.
    A smaller explicitly supplied batch also supports isolated fixture tests.
    Replays do not reopen successful/no-image/dead tasks of the same parser.
    """
    if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("图片任务分页参数无效。")
    if capture_mode not in BACKFILL_MODES:
        raise ValueError("补采模式无效。")
    if execute and (not getattr(settings, "image_backfill_enabled", False) or not workers_ready
                    or TaskType.PRODUCT_DETAIL not in settings.enabled_task_types):
        raise ValueError("需显式启用图片补采，并先升级所有Worker后声明workers_ready。")
    if not images_table_available(session):
        raise ValueError("需先启用商品图库扩展表。")
    shop_ids, scope_hash = _seed_scope(session, settings, batch)
    covered = exists().where(ProductImage.product_id == Product.id, ProductImage.is_current.is_(True))
    missing = ~covered
    if capture_mode == MEDIA_MODE:
        if not product_content_table_available(session):
            raise ValueError("需先启用商品介绍扩展表。")
        content = exists().where(ProductContent.product_id == Product.id, ProductContent.source_version == CONTENT_SOURCE_VERSION,
                                 ProductContent.completeness == "complete")
        missing = (~covered) | (~content)
    rows = session.execute(select(Product.id, Product.platform_product_id, Product.product_url, Product.shop_id,
                                   Shop.platform_shop_id).join(Shop, Product.shop_id == Shop.id)
                           .where(Product.platform == settings.platform, Product.region == settings.region,
                                  Product.shop_id.in_(shop_ids), Product.last_success_at.is_not(None),
                                  Product.pricing_context == "public_listed", Product.currency == settings.currency,
                                  Product.id > after_id, missing)
                           .order_by(Product.id).limit(limit)).all()
    key_builder = media_task_key if capture_mode == MEDIA_MODE else image_task_key
    keyed = [(row, key_builder(row.id, row.platform_product_id, settings.platform, settings.region)) for row in rows]
    existing = set(session.scalars(select(CrawlTask.idempotency_key).where(
        CrawlTask.idempotency_key.in_([key for _, key in keyed])))) if keyed else set()
    values = []
    now = database_now(session) if execute and rows else None
    for row, key in keyed:
        if key in existing:
            continue
        try:
            source = public_url(row.product_url)
            if product_id_from_url(source) != row.platform_product_id:
                raise ValueError
        except (TypeError, ValueError, PublicPageError):
            raise ValueError("已存商品链接与身份不一致，未创建本页图片任务。") from None
        values.append({"task_id": str(uuid4()), "idempotency_key": key,
                       "platform": settings.platform, "region": settings.region,
                       "task_type": TaskType.PRODUCT_DETAIL, "resource_type": ResourceType.PRODUCT,
                       "resource_id": str(row.id), "priority": 3, "status": TaskStatus.PENDING,
                       "scheduled_for": now, "available_at": now,
                       "payload": {"schema_version": 1, "capture_mode": capture_mode,
                                   "image_source_version": IMAGE_SOURCE_VERSION, "scope_hash": scope_hash,
                                   **({"content_source_version": CONTENT_SOURCE_VERSION} if capture_mode == MEDIA_MODE else {}),
                                   "product_id": str(row.id), "platform_product_id": row.platform_product_id,
                                   "product_url": source, "shop_id": str(row.shop_id),
                                   "platform_shop_id": row.platform_shop_id, "pricing_context": "public_listed"}})
    created = 0
    if execute and values:
        statement = insert(CrawlTask).values(values)
        session.execute(statement.on_duplicate_key_update(idempotency_key=statement.inserted.idempotency_key))
        session.flush()
        # A competing planner may have inserted a key after our read. Its UUID
        # remains unchanged by the no-op upsert, so only our UUIDs count here.
        created = int(session.scalar(select(func.count()).select_from(CrawlTask).where(
            CrawlTask.task_id.in_([value["task_id"] for value in values]))) or 0)
    return {"mode": "execute" if execute else "dry_run", "capture_mode": capture_mode,
            "image_source_version": IMAGE_SOURCE_VERSION, "scope_hash": scope_hash,
            "seed_count": len(batch.rows), "shop_count": len(shop_ids), "scanned": len(rows),
            "existing_tasks": len(rows) - created if execute else len(existing),
            "planned_tasks": len(values), "created_tasks": created,
            "next_after_id": rows[-1].id if len(rows) == limit else None,
            "http_requests": 0}


def image_backfill_status(session, settings, batch) -> dict:
    """Read task progress and actual gallery coverage without exposing URLs."""
    ready = images_table_available(session)
    shop_ids, scope_hash = _seed_scope(session, settings, batch)
    clauses = (Product.platform == settings.platform, Product.region == settings.region, Product.shop_id.in_(shop_ids))
    current = exists().where(ProductImage.product_id == Product.id, ProductImage.is_current.is_(True))
    columns = [func.count(), func.coalesce(func.sum(case((Product.last_success_at.is_not(None), 1), else_=0)), 0)]
    if ready:
        columns.extend([func.coalesce(func.sum(case((current, 1), else_=0)), 0),
                        func.coalesce(func.sum(case((and_(current, Product.last_success_at.is_not(None)), 1), else_=0)), 0)])
    data = session.execute(select(*columns).select_from(Product).where(*clauses)).one()
    products, historical = int(data[0]), int(data[1])
    image_products, historical_images = (int(data[2]), int(data[3])) if ready else (None, None)
    task_query = select(capture_mode_expression(), CrawlTask.status, CrawlTask.checkpoint["outcome"].as_string(), func.count())\
        .select_from(Product).join(CrawlTask, and_(
            CrawlTask.resource_type == ResourceType.PRODUCT,
            CrawlTask.resource_id == func.cast(Product.id, CrawlTask.resource_id.type),
            CrawlTask.platform == settings.platform, CrawlTask.region == settings.region))\
        .where(*clauses, CrawlTask.task_type == TaskType.PRODUCT_DETAIL,
               capture_mode_expression().in_(BACKFILL_MODES),
               CrawlTask.payload["image_source_version"].as_string() == IMAGE_SOURCE_VERSION)\
        .group_by(capture_mode_expression(), CrawlTask.status, CrawlTask.checkpoint["outcome"].as_string())
    by_status = {value.value: 0 for value in TaskStatus}
    outcomes = {"available": 0, "no_images": 0, "skipped": 0}
    tasks_by_mode = {mode: 0 for mode in BACKFILL_MODES}
    for mode, task_status, outcome, count in session.execute(task_query):
        tasks_by_mode[mode] += int(count)
        by_status[task_status.value] += int(count)
        if task_status == TaskStatus.SUCCEEDED:
            if outcome in {"available", "no_images"}:
                outcomes[outcome] += int(count)
            elif outcome in {"skipped", "skipped_already_available", "already_available"}:
                outcomes["skipped"] += int(count)
    active = sum(by_status[value.value] for value in ACTIVE_IMAGE_STATUSES)
    total_tasks = sum(by_status.values())
    content_ready = product_content_table_available(session)
    content_counts = dict(session.execute(select(ProductContent.completeness, func.count()).join(
        Product, Product.id == ProductContent.product_id).where(*clauses,
        ProductContent.source_version == CONTENT_SOURCE_VERSION).group_by(ProductContent.completeness)).all()) if content_ready else {}
    content_captured = sum(int(count) for count in content_counts.values())
    historical_content_counts = dict(session.execute(select(ProductContent.completeness, func.count()).join(
        Product, Product.id == ProductContent.product_id).where(*clauses, Product.last_success_at.is_not(None),
        ProductContent.source_version == CONTENT_SOURCE_VERSION).group_by(ProductContent.completeness)).all()) if content_ready else {}
    needs_content = tasks_by_mode[MEDIA_MODE] > 0
    content_complete = content_ready and int(historical_content_counts.get("complete", 0)) == historical
    status = "pending_schema" if not ready else "not_started" if not total_tasks else "running" if active else (
        "completed" if historical_images == historical and (not needs_content or content_complete)
                       and not by_status["dead"] and not by_status["cancelled"] else "partial")
    return {"status": status, "enabled": bool(getattr(settings, "image_backfill_enabled", False)),
            "schema_ready": ready, "capture_modes": list(BACKFILL_MODES), "capture_mode": MEDIA_MODE, "image_source_version": IMAGE_SOURCE_VERSION,
            "content_schema_ready": content_ready, "content_source_version": CONTENT_SOURCE_VERSION,
            "content_captured_products": content_captured, "content_status_counts": content_counts,
            "historical_content_status_counts": historical_content_counts,
            "historical_details_missing_content": historical - sum(int(count) for count in historical_content_counts.values()) if content_ready else None,
            "historical_details_incomplete_content": historical - int(historical_content_counts.get("complete", 0)) if content_ready else None,
            "tasks_by_mode": tasks_by_mode,
            "scope_hash": scope_hash, "seed_count": len(batch.rows), "shop_count": len(shop_ids),
            "products": products, "historical_detail_products": historical,
            "products_with_current_images": image_products, "historical_details_with_images": historical_images,
            "historical_details_missing_images": historical - historical_images if ready else None,
            "image_coverage_pct": str(round(image_products * 100 / products, 2)) if ready and products else None,
            "tasks_total": total_tasks, "active": active, "dead": by_status["dead"],
            **outcomes, "task_statuses": by_status,
            "coverage_basis": "actual_current_product_images", "http_requests": 0}
