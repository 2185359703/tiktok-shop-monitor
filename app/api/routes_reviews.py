"""Stored public reviews and an authenticated collection request."""

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.api.routes import After, Db, Limit, require_write_token, scope
from app.models import CrawlTask, Product, ProductReview, TaskStatus, TaskType
from app.services.product_reviews import ensure_review_task, reviews_available


from app.api.async_router import AsyncAPIRouter


router = AsyncAPIRouter(prefix="/api/v1")


@router.get("/products/{product_id}/reviews")
def product_reviews(product_id: int, request: Request, db: Db, limit: Limit = 20, after_id: After = 0):
    product = db.scalar(select(Product).where(Product.id == product_id, *scope(Product, request)))
    if product is None:
        raise HTTPException(404, "product_not_found")
    if not reviews_available(db):
        return {"product_id": str(product_id), "readiness": False, "status": "pending_schema", "items": [],
                "collected_count": "0", "next_after_id": None, "collection_complete": False, "latest_task": None}
    rows = db.scalars(select(ProductReview).where(ProductReview.product_id == product_id, ProductReview.id > after_id)
                      .order_by(ProductReview.id).limit(limit + 1)).all()
    count = db.scalar(select(func.count()).select_from(ProductReview).where(ProductReview.product_id == product_id))
    latest = db.scalar(select(CrawlTask).where(*scope(CrawlTask, request), CrawlTask.task_type == TaskType.PRODUCT_REVIEWS,
        CrawlTask.resource_id == str(product_id)).order_by(CrawlTask.scheduled_for.desc(), CrawlTask.id.desc()).limit(1))
    progress = (latest.checkpoint or {}) if latest else {}
    complete = bool(latest and latest.status == TaskStatus.SUCCEEDED and progress.get("complete") is True)
    status = "complete" if complete else "partial" if count else "not_captured"
    if latest and latest.status == TaskStatus.DEAD:
        status = "partial_failed" if count else "failed"
    elif latest and latest.status in {TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRY_WAITING, TaskStatus.ABANDONED}:
        status = "partial_pending" if count else "pending"
    fields = ("platform_review_id", "platform_variant_id", "rating", "text", "reviewer_name", "reviewed_at",
              "sku_specification", "country", "verified_purchase", "incentivized", "images", "last_seen_at", "source_kind")
    return {"product_id": str(product_id), "readiness": True, "status": status,
            "items": [{"id": str(row.id), **{key: getattr(row, key) for key in fields}} for row in rows[:limit]],
            "collected_count": str(count), "source_total": progress.get("source_total", (latest.payload or {}).get("source_total")) if latest else None,
            "source_scope": "global", "collection_complete": complete,
            "next_after_id": str(rows[limit - 1].id) if len(rows) > limit else None,
            "latest_task": {"task_id": latest.task_id, "status": latest.status,
                            "page_number": latest.payload.get("page_number"), "attempt_count": latest.attempt_count} if latest else None}


class ReviewCollectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    max_pages: int = Field(default=50, ge=1, le=500)


@router.post("/products/{product_id}/reviews/collect", status_code=202, dependencies=[Depends(require_write_token)])
def collect_reviews(product_id: int, body: ReviewCollectionRequest, request: Request, db: Db):
    settings = request.app.state.settings
    if TaskType.PRODUCT_REVIEWS not in settings.enabled_task_types:
        raise HTTPException(409, "review_collection_disabled")
    if not reviews_available(db):
        raise HTTPException(503, "review_schema_not_ready")
    try:
        task, created = ensure_review_task(db, settings, product_id, max_pages=body.max_pages)
        db.commit()
    except ValueError as error:
        db.rollback()
        raise HTTPException(404 if str(error) == "product_not_found" else 422,
                            "product_not_found" if str(error) == "product_not_found" else "invalid_review_product") from None
    return {"task_id": task.task_id, "product_id": str(product_id), "created": created}
