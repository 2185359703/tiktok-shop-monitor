"""Explicit source collection launch API; all writes use the existing write gate."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from pydantic import Field, StrictInt, StringConstraints, field_validator
from sqlalchemy import select

from app.api.async_router import AsyncAPIRouter
from app.api.routes import Db, require_write_token, task_summary
from app.api.routes_collection_management import Before, Identifier, Input, Limit, Name, ResourceIds, page, perform
from app.models import CrawlTask, TaskType
from app.models.collection_launch import CollectionLaunch, CollectionLaunchTask
from app.services.collection_management import market
from app.services.collection_intake import create_launch, launch_summary


router = AsyncAPIRouter(prefix="/api/v1/collection-management", tags=["collection-management"])
ShopUrl = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2048)]


class LaunchInput(Input):
    request_id: UUID
    name: Name
    task_type: Literal[TaskType.SHOP_DISCOVERY, TaskType.PRODUCT_DISCOVERY, TaskType.PRODUCT_DETAIL, TaskType.PRODUCT_REVIEWS]
    resource_ids: ResourceIds | None = None
    shop_urls: Annotated[list[ShopUrl], Field(min_length=1, max_length=100)] | None = None
    max_pages: Annotated[StrictInt, Field(ge=1, le=500)] | None = None

    # Numeric range and uniqueness match plans; keep unsigned BIGINT as strings.
    @field_validator("resource_ids")
    @classmethod
    def resources(cls, values):
        if values is not None and any(int(value) > 2 ** 64 - 1 for value in values):
            raise ValueError("invalid_resource_ids")
        return sorted(set(values), key=int) if values is not None else None


@router.post("/launches", dependencies=[Depends(require_write_token)])
def launch(body: LaunchInput, request: Request):
    def create(db, settings):
        row, created = create_launch(db, settings, body.model_dump(mode="json", exclude={"request_id"}), body.request_id)
        return {"created": created, "launch": launch_summary(db, row)}
    return perform(request, create)


@router.get("/launches")
def launches(request: Request, db: Db, limit: Limit = 20, before_id: Before = None,
             task_type: Literal["shop_discovery", "product_discovery", "product_detail", "product_reviews"] | None = None):
    query = select(CollectionLaunch).where(*market(CollectionLaunch, request.app.state.settings))
    if task_type is not None:
        query = query.where(CollectionLaunch.task_type == TaskType(task_type))
    rows, cursor = page(db, query, CollectionLaunch, limit, before_id)
    return {"items": [launch_summary(db, row) for row in rows], "next_before_id": cursor}


@router.get("/launches/{launch_id}")
def launch_detail(launch_id: Identifier, request: Request, db: Db, limit: Limit = 20, before_id: Before = None):
    row = db.scalar(select(CollectionLaunch).where(CollectionLaunch.id == launch_id,
        *market(CollectionLaunch, request.app.state.settings)))
    if row is None:
        raise HTTPException(404, "collection_launch_not_found")
    query = select(CrawlTask, CollectionLaunchTask.member_role).join(CollectionLaunchTask,
        CollectionLaunchTask.task_id == CrawlTask.id).where(CollectionLaunchTask.launch_id == row.id)
    if before_id is not None:
        query = query.where(CrawlTask.id < before_id)
    tasks = db.execute(query.order_by(CrawlTask.id.desc()).limit(limit + 1)).all()
    return {**launch_summary(db, row), "tasks": [{**task_summary(task), "member_role": role} for task, role in tasks[:limit]],
        "next_before_id": str(tasks[limit - 1][0].id) if len(tasks) > limit else None}


@router.post("/launches/{launch_id}/cancel", dependencies=[Depends(require_write_token)])
def cancel(launch_id: Identifier, body: Input, request: Request):
    from app.services.collection_launch_control import cancel_launch
    return perform(request, cancel_launch, launch_id)
