import secrets
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select

from app.models import ChangeEvent, ChangeEventType, CrawlAttempt, CrawlTask, Product, ProductVariant, ProductSnapshot, VariantSnapshot, ResolutionStatus, SeedShopObservation, Shop, TaskStatus, TaskType
from app.services.scheduler import task_counts
from app.services.seed_importer import ensure_seed_task, seed_task_key
from app.services.shop_discovery import ensure_product_discovery_task, product_discovery_key
from app.crawlers.public_shop import shop_id_from_url
from app.storage.database import session_scope
from app.workers.handlers import handler_registry
from app.services.task_runtime import database_now
from app.services.product_detail import ensure_initial_detail_task
from app.crawlers.product_list import product_id_from_url
from app.services.snapshot_service import ensure_snapshot_task, ensure_change_task, utc_window


router = APIRouter(prefix="/api/v1")


def session_dependency(request: Request):
    with request.app.state.factory() as session:
        yield session


Db = Annotated[object, Depends(session_dependency)]
Limit = Annotated[int, Query(ge=1, le=100)]
After = Annotated[int, Query(ge=0)]


def scope(model, request):
    settings = request.app.state.settings
    return model.platform == settings.platform, model.region == settings.region


def task_summary(task):
    progress = {key: value for key, value in (task.checkpoint or {}).items()
                if key in {"page_number", "page_products", "new_products", "new_detail_tasks", "has_more", "complete", "next_task_created",
                           "detail_updated", "stale_observation_skipped", "variants_seen", "variants_upserted", "new_variants", "attribute_conflicts",
                           "snapshots_written", "variant_snapshots_written", "events_detected", "events_written", "baseline", "comparison_skipped", "window_already_saved", "change_task_created", "images_upserted", "gallery_complete", "content_saved", "content_complete", "content_nodes", "content_parameters", "profile_updated", "product_count_updated"}
                and type(value) in {int, bool}}
    if (task.checkpoint or {}).get("stop_reason") in {"page_saved", "source_exhausted", "page_budget"}:
        progress["stop_reason"] = task.checkpoint["stop_reason"]
    if (task.checkpoint or {}).get("observation_status") in {"complete", "partial"}:
        progress["observation_status"] = task.checkpoint["observation_status"]
    for key in ("snapshot_id", "previous_snapshot_id"):
        value = (task.checkpoint or {}).get(key)
        if value is None or isinstance(value, str) and value.isdecimal():
            if key in (task.checkpoint or {}): progress[key] = value
    return {"id": str(task.id), "task_id": task.task_id, "task_type": task.task_type,
            "resource_type": task.resource_type, "resource_id": task.resource_id,
            "status": task.status, "attempt_count": task.attempt_count, "max_attempts": task.max_attempts,
            "priority": task.priority, "available_at": task.available_at, "scheduled_for": task.scheduled_for,
            "started_at": task.started_at, "finished_at": task.finished_at,
            "worker_id": task.worker_id, "lease_expires_at": task.lease_expires_at, "progress": progress,
            "capture_mode": (task.payload or {}).get("capture_mode", "full") if (task.payload or {}).get("capture_mode", "full") in {"full", "images_only", "media_content_only"} else "unknown"}


@router.get("/capabilities")
def capabilities(request: Request):
    implemented = handler_registry()
    return {"platform": request.app.state.settings.platform, "region": request.app.state.settings.region,
            "implemented_task_types": sorted(implemented),
            "enabled_task_types": request.app.state.settings.enabled_task_types,
            "product_detail_capture_modes": ["full", "images_only", "media_content_only"],
            "image_backfill_enabled": request.app.state.settings.image_backfill_enabled,
            "unimplemented_task_types": [kind for kind in TaskType if kind not in implemented],
            "writes_enabled": request.app.state.settings.api_token is not None}


@router.get("/overview")
def overview(request: Request, db: Db):
    return {"shops": db.scalar(select(func.count()).select_from(Shop).where(*scope(Shop, request))),
            "shops_with_url": db.scalar(select(func.count()).select_from(Shop).where(*scope(Shop, request), Shop.canonical_url.is_not(None))),
            "products": db.scalar(select(func.count()).select_from(Product).where(*scope(Product, request))),
            "tasks": [{"task_type": kind, "status": status, "count": count}
                      for kind, status, count in task_counts(db, request.app.state.settings)]}


@router.get("/shops")
def shops(request: Request, db: Db, limit: Limit = 20, after_id: After = 0, with_url: bool = True):
    query = select(Shop).where(*scope(Shop, request), Shop.id > after_id)
    if with_url:
        query = query.where(Shop.canonical_url.is_not(None))
    rows = db.scalars(query.order_by(Shop.id).limit(limit)).all()
    return {"items": [{"id": str(row.id), "platform_shop_id": row.platform_shop_id,
                       "name": row.canonical_name, "shop_url": row.canonical_url,
                       "discovery_category": row.discovery_category, "currency": row.currency,
                       "status": row.status, "last_seen_at": row.last_seen_at} for row in rows],
            "next_after_id": str(rows[-1].id) if len(rows) == limit else None}


@router.get("/products")
def products(request: Request, db: Db, limit: Limit = 20, after_id: After = 0, shop_id: Annotated[int | None, Query(ge=1)] = None):
    query = select(Product).where(*scope(Product, request), Product.id > after_id)
    if shop_id is not None:
        query = query.where(Product.shop_id == shop_id)
    rows = db.scalars(query.order_by(Product.id).limit(limit)).all()
    return {"items": [{"id": str(row.id), "platform_product_id": row.platform_product_id,
                       "shop_id": str(row.shop_id), "title": row.title, "product_url": row.product_url,
                       "category": row.normalized_category, "currency": row.currency,
                       "pricing_context": row.pricing_context,
                       "price_min": str(row.price_min) if row.price_min is not None else None,
                       "price_max": str(row.price_max) if row.price_max is not None else None,
                       "original_price_min": str(row.original_price_min) if row.original_price_min is not None else None,
                       "original_price_max": str(row.original_price_max) if row.original_price_max is not None else None,
                       "discount_rate": str(row.discount_rate) if row.discount_rate is not None else None,
                       "price_text": row.price_text, "brand": row.brand,
                       "rating": str(row.rating) if row.rating is not None else None,
                       "review_count": row.review_count, "sales_count": row.sales_count,
                       "sales_count_text": row.sales_count_text,
                       "stock_status": row.stock_status, "availability_status": row.availability_status,
                       "parser_version": row.parser_version, "data_version": row.data_version,
                       "last_seen_at": row.last_seen_at, "last_success_at": row.last_success_at} for row in rows],
            "next_after_id": str(rows[-1].id) if len(rows) == limit else None}


@router.get("/tasks")
def tasks(request: Request, db: Db, limit: Limit = 20, after_id: After = 0,
          status: TaskStatus | None = None, task_type: TaskType | None = None):
    query = select(CrawlTask).where(*scope(CrawlTask, request), CrawlTask.id > after_id)
    if status is not None:
        query = query.where(CrawlTask.status == status)
    if task_type is not None:
        query = query.where(CrawlTask.task_type == task_type)
    rows = db.scalars(query.order_by(CrawlTask.id).limit(limit)).all()
    return {"items": [task_summary(row) for row in rows],
            "next_after_id": str(rows[-1].id) if len(rows) == limit else None}


@router.get("/products/{product_id}")
def product_detail(product_id: int, request: Request, db: Db):
    product = db.scalar(select(Product).where(Product.id == product_id, *scope(Product, request)))
    if product is None: raise HTTPException(404, "product_not_found")
    return {"id": str(product.id), "platform_product_id": product.platform_product_id, "title": product.title,
            "shop_id": str(product.shop_id), "shop_name": product.shop.canonical_name,
            "product_url": product.product_url, "currency": product.currency, "pricing_context": product.pricing_context,
            "price_min": str(product.price_min) if product.price_min is not None else None,
            "price_max": str(product.price_max) if product.price_max is not None else None,
            "original_price_min": str(product.original_price_min) if product.original_price_min is not None else None,
            "original_price_max": str(product.original_price_max) if product.original_price_max is not None else None,
            "discount_rate": str(product.discount_rate) if product.discount_rate is not None else None,
            "rating": str(product.rating) if product.rating is not None else None,
            "review_count": product.review_count, "sales_count": product.sales_count,
            "sales_count_text": product.sales_count_text,
            "stock_status": product.stock_status, "availability_status": product.availability_status,
            "first_seen_at": product.first_seen_at, "last_seen_at": product.last_seen_at,
            "description": product.description, "original_category": product.original_category,
            "normalized_category": product.normalized_category,
            "brand": product.brand, "material": product.material, "gender": product.gender, "season": product.season,
            "style": product.style, "fit": product.fit, "garment_length": product.garment_length, "pattern": product.pattern,
            "colors": product.color_summary, "sizes": product.size_summary,
            "attribute_conflicts": (product.raw_attributes or {}).get("attribute_conflicts", {}),
            "retained_attributes": (product.raw_attributes or {}).get("retained_attributes", []),
            "last_success_at": product.last_success_at, "parser_version": product.parser_version,
            "content_hash": product.content_hash, "data_version": product.data_version}


@router.get("/products/{product_id}/variants")
def variants(product_id: int, request: Request, db: Db, limit: Limit = 20, after_id: After = 0):
    product = db.scalar(select(Product).where(Product.id == product_id, *scope(Product, request)))
    if product is None: raise HTTPException(404, "product_not_found")
    latest = (product.raw_attributes or {}).get("observed_sku_ids")
    seen = set(latest) if isinstance(latest, list) else None
    rows = db.scalars(select(ProductVariant).where(ProductVariant.product_id == product.id, ProductVariant.id > after_id)
                      .order_by(ProductVariant.id).limit(limit)).all()
    return {"items": [{"id": str(row.id), "platform_variant_id": row.platform_variant_id,
                       "color": row.color, "size": row.size, "material": row.material, "options": row.options,
                       "currency": row.currency, "price": str(row.price) if row.price is not None else None,
                       "original_price": str(row.original_price) if row.original_price is not None else None,
                       "stock_status": row.stock_status, "available_quantity": row.available_quantity,
                       "seen_in_latest_detail": row.platform_variant_id in seen if seen is not None else None,
                       "updated_at": row.updated_at} for row in rows],
            "next_after_id": str(rows[-1].id) if len(rows) == limit else None}


@router.get("/tasks/{task_id}/attempts")
def attempts(task_id: str, request: Request, db: Db):
    task = db.scalar(select(CrawlTask).where(*scope(CrawlTask, request), CrawlTask.task_id == task_id))
    if task is None:
        raise HTTPException(404, "task_not_found")
    rows = db.scalars(select(CrawlAttempt).where(CrawlAttempt.task_id == task.id).order_by(CrawlAttempt.attempt_no)).all()
    # Deliberately exclude legacy error text and payloads, which may be entered manually.
    return {"task": task_summary(task), "attempts": [
        {"attempt_no": row.attempt_no, "worker_id": row.worker_id, "response_status": row.response_status,
         "latency_ms": row.latency_ms, "succeeded": row.succeeded, "error_type": row.error_type,
         "started_at": row.started_at, "finished_at": row.finished_at} for row in rows]}


@router.get("/products/{product_id}/snapshots")
def snapshots(product_id: int, request: Request, db: Db, limit: Limit = 20, after_id: After = 0):
    product = db.scalar(select(Product).where(Product.id == product_id, *scope(Product, request)))
    if product is None:
        raise HTTPException(404, "product_not_found")
    rows = db.scalars(select(ProductSnapshot).where(ProductSnapshot.product_id == product_id,
                                                   ProductSnapshot.id > after_id).order_by(ProductSnapshot.id).limit(limit)).all()
    return {"items": [{"id": str(row.id), "scheduled_for": row.scheduled_for, "captured_at": row.captured_at, "currency": row.currency,
                       "pricing_context": row.pricing_context,
                       "price_min": str(row.price_min) if row.price_min is not None else None,
                       "price_max": str(row.price_max) if row.price_max is not None else None,
                       "original_price_min": str(row.original_price_min) if row.original_price_min is not None else None,
                       "original_price_max": str(row.original_price_max) if row.original_price_max is not None else None,
                       "discount_rate": str(row.discount_rate) if row.discount_rate is not None else None,
                       "stock_status": row.stock_status, "availability_status": row.availability_status,
                       "variant_count": row.variant_count, "parser_version": row.parser_version,
                       "source_status": row.source_status, "source_url": row.source_url} for row in rows],
            "next_after_id": str(rows[-1].id) if len(rows) == limit else None}


@router.get("/products/{product_id}/snapshots/{snapshot_id}/variants")
def snapshot_variants(product_id: int, snapshot_id: int, request: Request, db: Db, limit: Limit = 20, after_id: After = 0):
    snapshot = db.scalar(select(ProductSnapshot).join(Product, Product.id == ProductSnapshot.product_id).where(
        *scope(Product, request), Product.id == product_id, ProductSnapshot.id == snapshot_id))
    if snapshot is None: raise HTTPException(404, "snapshot_not_found")
    rows = db.scalars(select(VariantSnapshot).where(VariantSnapshot.product_snapshot_id == snapshot.id,
                       VariantSnapshot.id > after_id).order_by(VariantSnapshot.id).limit(limit)).all()
    return {"items": [{"id": str(row.id), "variant_id": str(row.variant_id), "captured_at": row.captured_at,
                       "currency": row.currency, "price": str(row.price) if row.price is not None else None,
                       "original_price": str(row.original_price) if row.original_price is not None else None,
                       "stock_status": row.stock_status, "available_quantity": row.available_quantity} for row in rows],
            "next_after_id": str(rows[-1].id) if len(rows) == limit else None}


@router.get("/events")
def events(request: Request, db: Db, limit: Limit = 20, after_id: After = 0,
           product_id: Annotated[int | None, Query(ge=1)] = None, event_type: ChangeEventType | None = None):
    query = select(ChangeEvent).join(Product, Product.id == ChangeEvent.product_id).where(
        *scope(Product, request), ChangeEvent.id > after_id)
    if product_id is not None: query = query.where(ChangeEvent.product_id == product_id)
    if event_type is not None: query = query.where(ChangeEvent.event_type == event_type)
    rows = db.scalars(query.order_by(ChangeEvent.id).limit(limit)).all()
    return {"items": [{"id": str(row.id), "product_id": str(row.product_id), "event_type": row.event_type,
                       "variant_id": str(row.variant_id) if row.variant_id is not None else None,
                       "snapshot_id": str(row.snapshot_id),
                       "previous_snapshot_id": str(row.previous_snapshot_id) if row.previous_snapshot_id is not None else None,
                       "old_value": row.old_value, "new_value": row.new_value, "detected_at": row.detected_at} for row in rows],
            "next_after_id": str(rows[-1].id) if len(rows) == limit else None}


def require_write_token(request: Request, authorization: Annotated[str | None, Header()] = None):
    token = request.app.state.settings.api_token
    if token is None:
        raise HTTPException(503, "write_api_disabled")
    if not authorization or not secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "invalid_api_token")


class CreateTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_type: TaskType
    resource_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    scheduled_for: datetime | None = None

    @field_validator("scheduled_for")
    @classmethod
    def utc_schedule(cls, value):
        if value is not None:
            if value.tzinfo is None: raise ValueError("快照窗口需要显式时区。")
            return value.astimezone(timezone.utc)
        return value

    @model_validator(mode="after")
    def only_snapshots_accept_window(self):
        if self.scheduled_for is not None and self.task_type != TaskType.PRODUCT_SNAPSHOT:
            raise ValueError("scheduled_for 仅用于商品快照。")
        return self

    @field_validator("resource_id")
    @classmethod
    def resource_fits_database(cls, value):
        if int(value) > 2 ** 64 - 1:
            raise ValueError("资源标识超出数据库范围。")
        return value


@router.post("/tasks", dependencies=[Depends(require_write_token)])
def create_task(body: CreateTask, request: Request):
    if body.task_type not in handler_registry():
        raise HTTPException(501, "collector_not_implemented")
    if body.task_type not in request.app.state.settings.enabled_task_types:
        raise HTTPException(409, "collector_not_enabled")
    with session_scope(request.app.state.factory) as db:
        if body.task_type == TaskType.REPORT_GENERATION:
            raise HTTPException(422, "use_reports_jobs_endpoint")
        if body.task_type in {TaskType.PRODUCT_SNAPSHOT, TaskType.CHANGE_DETECTION}:
            product = db.scalar(select(Product).where(Product.id == int(body.resource_id), *scope(Product, request)).with_for_update())
            if product is None: raise HTTPException(404, "product_not_found")
            if body.task_type == TaskType.PRODUCT_SNAPSHOT:
                window = body.scheduled_for or utc_window(database_now(db), request.app.state.settings.snapshot_interval_seconds)
                try:
                    task, created = ensure_snapshot_task(db, product, window)
                except ValueError:
                    raise HTTPException(409, "product_not_ready_for_snapshot") from None
            else:
                snapshot = db.scalar(select(ProductSnapshot).where(ProductSnapshot.product_id == product.id,
                    ProductSnapshot.pricing_context == "public_listed").order_by(ProductSnapshot.captured_at.desc(), ProductSnapshot.id.desc()).limit(1))
                if snapshot is None: raise HTTPException(409, "product_has_no_snapshot")
                task, created = ensure_change_task(db, product, snapshot)
            return {"created": created, "task": task_summary(task)}
        if body.task_type == TaskType.PRODUCT_DETAIL:
            product = db.scalar(select(Product).where(Product.id == int(body.resource_id), *scope(Product, request)).with_for_update())
            if product is None: raise HTTPException(404, "product_not_found")
            if product_id_from_url(product.product_url) != product.platform_product_id:
                raise HTTPException(409, "product_url_not_verified")
            shop = db.get(Shop, product.shop_id)
            task, created = ensure_initial_detail_task(db, product, shop)
            return {"created": created, "task": task_summary(task)}
        if body.task_type == TaskType.PRODUCT_DISCOVERY:
            shop = db.scalar(select(Shop).where(Shop.id == int(body.resource_id), *scope(Shop, request)).with_for_update())
            if shop is None:
                raise HTTPException(404, "shop_not_found")
            if not shop.canonical_url or shop_id_from_url(shop.canonical_url) != shop.platform_shop_id:
                raise HTTPException(409, "shop_url_not_verified")
            created = ensure_product_discovery_task(db, shop, shop.canonical_url, database_now(db))
            task = db.scalar(select(CrawlTask).where(CrawlTask.idempotency_key == product_discovery_key(shop)))
            return {"created": created, "task": task_summary(task)}
        seed = db.scalar(select(SeedShopObservation).where(
            SeedShopObservation.id == int(body.resource_id), *scope(SeedShopObservation, request),
        ).with_for_update())
        if seed is None:
            raise HTTPException(404, "seed_not_found")
        if seed.resolution_status != ResolutionStatus.PENDING:
            raise HTTPException(409, "seed_already_processed")
        created = ensure_seed_task(db, seed, database_now(db))
        task = db.scalar(select(CrawlTask).where(CrawlTask.idempotency_key == seed_task_key(seed.row_hash)))
        # Replays never reopen cancelled/dead/successful tasks.
        return {"created": created, "task": task_summary(task)}
