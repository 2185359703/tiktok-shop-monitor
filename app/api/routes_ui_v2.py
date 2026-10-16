"""Small UI resources and nonblocking, persisted analysis snapshots."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.api.routes import shop_summary, task_summary
from app.api.routes_analytics import analytics_filters
from app.models import CrawlTask, Shop, TaskType, ProductMatch, SkuMatch
from app.models.analytics_read import AnalyticsSnapshot
from app.services.analytics_queries import list_events, list_products, parameters
from app.services.analytics_refresh import VERSION, enabled, key_for
from app.services.analytics_runtime import ready, source_reader
from app.services.analytics_snapshot import Busy, read_snapshot
from app.storage.async_database import app_call


from app.api.async_router import AsyncAPIRouter


router = AsyncAPIRouter(prefix="/api/v2", tags=["ui"])


def require_enabled(request):
    if not enabled():
        raise HTTPException(503, "analytics_not_enabled")


def require_ready(request, db):
    require_enabled(request)
    if not ready(db, request.app.state.settings):
        raise HTTPException(503, "analytics_backfill_pending")


async def snapshot_response(request, kind, options=None, **extra):
    require_enabled(request)
    params = parameters(request.app.state.settings, options, **extra)
    def read():
        with source_reader(request.app.state.factory, write=True) as db:
            require_ready(request, db)
            return read_snapshot(db, kind, params)
    try:
        cache = getattr(request.app.state, "ui_snapshot_cache", None)
        if cache is None:
            status, result = await app_call(request, read)
        else:
            status, result = await cache.get_or_build(key_for(kind, params), kind, lambda: app_call(request, read))
    except Busy:
        return JSONResponse({"detail": "analytics_busy"}, status_code=429, headers={"Retry-After": "10"})
    return JSONResponse(result, status_code=status, headers={"Cache-Control": "no-store", **({"Retry-After": "2"} if status == 202 else {})})


@router.get("/ui/bootstrap")
async def bootstrap(request: Request, initial: Literal["none", "workbench"] = "none"):
    available = False
    if enabled():
        def read_ready():
            with source_reader(request.app.state.factory) as db:
                return ready(db, request.app.state.settings)
        available = await app_call(request, read_ready)
    result = {"schema_version": 2, "enabled": enabled(), "ready": available,
              "pages": {key: available for key in ("products", "shops", "analytics", "events", "reports")}}
    if available and initial == "workbench":
        # Reuse the published generation while the browser downloads its JS.
        # A missing snapshot stays pending; bootstrap never runs an aggregate.
        response = await snapshot_response(request, "workbench", lookback_hours=24)
        if response.status_code == 200:
            import json
            result["initial_workbench"] = json.loads(response.body)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/catalog/products")
def products(request: Request, options: Annotated[dict, Depends(analytics_filters)],
             page: Annotated[int, Query(ge=1)] = 1, page_size: Annotated[int, Query(ge=1, le=100)] = 24,
             sort: str = "recent"):
    with source_reader(request.app.state.factory) as db:
        require_ready(request, db)
        try:
            return list_products(db, parameters(request.app.state.settings, options), page=page, page_size=page_size, sort=sort)
        except ValueError:
            raise HTTPException(422, "invalid_pagination") from None


@router.get("/catalog/stats")
async def product_stats(request: Request, options: Annotated[dict, Depends(analytics_filters)]):
    return await snapshot_response(request, "catalog_stats", options)


@router.get("/catalog/shops")
def shops(request: Request, page: Annotated[int, Query(ge=1)] = 1,
          page_size: Annotated[int, Query(ge=1, le=100)] = 20):
    with source_reader(request.app.state.factory) as db:
        require_ready(request, db)
        config = request.app.state.settings
        rows = db.scalars(select(Shop).where(Shop.platform == config.platform, Shop.region == config.region,
            Shop.canonical_url.is_not(None)).order_by(Shop.id).offset((page - 1) * page_size).limit(page_size + 1)).all()
        return {"items": [shop_summary(row, config) for row in rows[:page_size]], "page": page, "has_more": len(rows) > page_size}


@router.get("/analytics/overview")
async def overview(request: Request, lookback_hours: Annotated[int, Query(ge=1, le=720)] = 24):
    return await snapshot_response(request, "overview", lookback_hours=lookback_hours)


@router.get("/analytics/workbench")
async def workbench(request: Request, options: Annotated[dict, Depends(analytics_filters)],
              lookback_hours: Annotated[int, Query(ge=1, le=720)] = 24,
              topic: Literal["market", "attributes", "stock", "competition", "quality"] = "market"):
    # Themes share one consistent published generation. Only their required fields
    # are sent; changing tabs never launches another full-scope aggregation.
    response = await snapshot_response(request, "workbench", options, lookback_hours=lookback_hours)
    if response.status_code != 200:
        return response
    import json
    data = json.loads(response.body)
    common = {"version", "generated_at", "scope", "window", "summary", "history_summary", "coverage", "notes", "categories", "shops", "_meta"}
    fields = {"market": {"price_histogram", "price_rating_scatter", "discount_buckets", "event_trend_daily"},
        "attributes": {"attribute_distributions", "color_size_matrix"},
        "stock": {"stock_distribution", "discount_buckets", "color_size_matrix"},
        "competition": set(), "quality": {"collection_quality", "history_summary", "events_by_type"}}
    return JSONResponse({k: v for k, v in data.items() if k in common | fields[topic]}, headers={"Cache-Control": "no-store"})


@router.get("/events")
def events(request: Request, lookback_hours: Annotated[int, Query(ge=1, le=720)] = 24,
           event_type: Annotated[str | None, Query(max_length=32)] = None,
           shop_id: Annotated[int | None, Query(ge=1)] = None, q: Annotated[str | None, Query(max_length=200)] = None,
           cursor: Annotated[str | None, Query(max_length=4096)] = None, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    with source_reader(request.app.state.factory) as db:
        require_ready(request, db)
        params = parameters(request.app.state.settings, dict(lookback_hours=lookback_hours, event_type=event_type, shop_id=shop_id, q=q))
        try:
            return list_events(db, request.app.state.settings, params, cursor=cursor, limit=limit)
        except ValueError:
            raise HTTPException(422, "invalid_cursor") from None


@router.get("/events/summary")
async def events_summary(request: Request, lookback_hours: Annotated[int, Query(ge=1, le=720)] = 24,
                   event_type: Annotated[str | None, Query(max_length=32)] = None,
                   shop_id: Annotated[int | None, Query(ge=1)] = None, q: Annotated[str | None, Query(max_length=200)] = None,
                   since: datetime | None = None, until: datetime | None = None,
                   feed_upper_sequence: Annotated[int | None, Query(ge=0)] = None):
    if (since is None) != (until is None) or (since is not None and (
        since.tzinfo is None or until.tzinfo is None or since > until or (until - since).total_seconds() > 720 * 3600)):
        raise HTTPException(422, "invalid_event_window")
    options = dict(lookback_hours=lookback_hours, event_type=event_type, shop_id=shop_id, q=q,
                   since=since.isoformat() if since else None, until=until.isoformat() if until else None,
                   feed_upper_sequence=feed_upper_sequence)
    return await snapshot_response(request, "events_summary", options)


@router.get("/tasks/summary")
async def tasks_summary(request: Request):
    return await snapshot_response(request, "task_counts")


@router.get("/matches/candidates")
async def candidates(request: Request, product_id: Annotated[int | None, Query(ge=1)] = None):
    return await snapshot_response(request, "matching_candidates", {"product_id": product_id})


@router.get("/matches/summary")
def matches_summary(request: Request):
    config = request.app.state.settings
    with source_reader(request.app.state.factory) as db:
        require_ready(request, db)
        clauses = (ProductMatch.platform == config.platform, ProductMatch.region == config.region, ProductMatch.status == "confirmed")
        count = db.scalar(select(func.count()).select_from(ProductMatch).where(*clauses))
        sku_count = db.scalar(select(func.count()).select_from(SkuMatch).join(ProductMatch,
            ProductMatch.id == SkuMatch.product_match_id).where(*clauses, SkuMatch.status == "confirmed"))
        snapshot = db.get(AnalyticsSnapshot, (VERSION, key_for("matching_candidates", parameters(config))))
        candidates = snapshot.payload if snapshot else {}
        return {"confirmed_product_pairs": count, "confirmed_sku_pairs": sku_count,
                "unconfirmed_candidates": candidates.get("unconfirmed_candidates"), "candidate_scan": candidates.get("scan"),
                "candidate_count_is_complete": not candidates.get("scan", {}).get("bounded", True)}


@router.get("/reports")
def reports(request: Request, after_id: Annotated[int, Query(ge=0)] = 0,
            limit: Annotated[int, Query(ge=1, le=100)] = 8):
    config = request.app.state.settings
    with source_reader(request.app.state.factory) as db:
        rows = db.scalars(select(CrawlTask).where(CrawlTask.platform == config.platform, CrawlTask.region == config.region,
            CrawlTask.task_type == TaskType.REPORT_GENERATION, CrawlTask.id > after_id)
            .with_hint(CrawlTask, "FORCE INDEX (ix_crawl_tasks_summary)", dialect_name="mysql")
            .order_by(CrawlTask.id).limit(limit + 1)).all()
        latest = db.scalar(select(CrawlTask).where(CrawlTask.platform == config.platform, CrawlTask.region == config.region,
            CrawlTask.task_type == TaskType.REPORT_GENERATION, CrawlTask.status == "succeeded")
            .with_hint(CrawlTask, "FORCE INDEX (ix_crawl_tasks_summary)", dialect_name="mysql")
            .order_by(CrawlTask.id.desc()).limit(1))
        archive = {**task_summary(latest), "summary": (latest.checkpoint or {}).get("report_summary"),
                   "report_as_of": (latest.checkpoint or {}).get("report_as_of"),
                   "lookback_hours": (latest.payload or {}).get("lookback_hours", 24)} if latest else None
        if archive and archive["summary"] is None:
            archive["summary"] = {"products": (latest.checkpoint or {}).get("report_products"),
                                  "events_in_window": (latest.checkpoint or {}).get("report_events")}
        return {"items": [{**task_summary(row), "summary": (row.checkpoint or {}).get("report_summary"),
                           "report_as_of": (row.checkpoint or {}).get("report_as_of")} for row in rows[:limit]],
                "latest_completed": archive,
                "next_after_id": str(rows[limit - 1].id) if len(rows) > limit else None}
