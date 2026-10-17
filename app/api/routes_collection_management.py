"""Authenticated control plane; request handlers only enqueue durable work."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints, field_validator, model_validator
from sqlalchemy import select

from app.api.async_router import AsyncAPIRouter
from app.api.routes import Db, require_write_token, task_summary
from app.models import CollectionOperation, CollectionPlan, CollectionRun, TaskType
from app.services import collection_management as management
from app.storage.database import session_scope


router = AsyncAPIRouter(prefix="/api/v1/collection-management", tags=["collection-management"])
Limit = Annotated[int, Query(ge=1, le=50)]
Before = Annotated[int | None, Query(ge=1, le=2 ** 64 - 1)]
Identifier = Annotated[int, Path(ge=1, le=2 ** 64 - 1)]
Name = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$")]
ResourceId = Annotated[str, StringConstraints(strict=True, pattern=r"^[1-9][0-9]{0,19}$")]
ResourceIds = Annotated[list[ResourceId], Field(min_length=1, max_length=100)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ControlInput(Input):
    paused: StrictBool


class PlanInput(Input):
    request_id: UUID
    name: Name
    task_type: Literal[TaskType.SHOP_PROFILE, TaskType.PRODUCT_SNAPSHOT]
    resource_ids: ResourceIds
    schedule_mode: Literal["once", "interval"] = "once"
    interval_seconds: StrictInt | None = None
    enabled: StrictBool = True

    @field_validator("resource_ids")
    @classmethod
    def resources(cls, values):
        if len(set(values)) != len(values) or any(int(value) > 2 ** 64 - 1 for value in values):
            raise ValueError("invalid_resource_ids")
        return sorted(values, key=int)


class PlanUpdate(Input):
    name: Name | None = None
    resource_ids: ResourceIds | None = None
    interval_seconds: StrictInt | None = None
    enabled: StrictBool | None = None

    @field_validator("resource_ids")
    @classmethod
    def resources(cls, values):
        return PlanInput.resources(values) if values is not None else values

    @model_validator(mode="after")
    def changed_fields(self):
        if not self.model_fields_set or any(getattr(self, name) is None for name in self.model_fields_set - {"interval_seconds"}):
            raise ValueError("invalid_plan_update")
        return self


class RunInput(Input):
    request_id: UUID


def perform(request, function, *args):
    try:
        with session_scope(request.app.state.factory) as session:
            return function(session, request.app.state.settings, *args)
    except management.ManagementError as error:
        raise HTTPException(error.status, error.code) from None


def known_plan(db, settings, plan_id):
    try:
        return management.get_plan(db, settings, plan_id)
    except management.ManagementError as error:
        raise HTTPException(error.status, error.code) from None


def page(db, query, model, limit, before_id):
    if before_id is not None:
        query = query.where(model.id < before_id)
    rows = db.scalars(query.order_by(model.id.desc()).limit(limit + 1)).all()
    return rows[:limit], str(rows[limit - 1].id) if len(rows) > limit else None


@router.get("/control")
def control(request: Request, db: Db):
    return management.read_control(db, request.app.state.settings)


@router.put("/control", dependencies=[Depends(require_write_token)])
def update_control(body: ControlInput, request: Request):
    return perform(request, management.set_control, body.paused)


@router.get("/plans")
def plans(request: Request, db: Db, limit: Limit = 20, before_id: Before = None):
    rows, cursor = page(db, select(CollectionPlan).where(*management.market(CollectionPlan, request.app.state.settings)),
                        CollectionPlan, limit, before_id)
    return {"items": [management.plan_summary(row) for row in rows], "next_before_id": cursor}


@router.post("/plans", dependencies=[Depends(require_write_token)])
def create_plan(body: PlanInput, request: Request):
    def create(session, settings):
        plan, created = management.create_plan(session, settings, body.model_dump(mode="json", exclude={"request_id"}), body.request_id)
        return {"created": created, "plan": management.plan_summary(plan)}
    return perform(request, create)


@router.get("/plans/{plan_id}")
def plan(plan_id: Identifier, request: Request, db: Db):
    return management.plan_summary(known_plan(db, request.app.state.settings, plan_id))


@router.patch("/plans/{plan_id}", dependencies=[Depends(require_write_token)])
def update_plan(plan_id: Identifier, body: PlanUpdate, request: Request):
    def update(session, settings):
        return management.plan_summary(management.update_plan(session, settings, plan_id, body.model_dump(exclude_unset=True)))
    return perform(request, update)


@router.post("/plans/{plan_id}/run", dependencies=[Depends(require_write_token)])
def run_plan(plan_id: Identifier, body: RunInput, request: Request):
    def run(session, settings):
        row, created = management.run_plan(session, settings, plan_id, body.request_id)
        return {"created": created, "run": management.run_summary(session, row, include_tasks=True)}
    return perform(request, run)


@router.get("/plans/{plan_id}/runs")
def runs(plan_id: Identifier, request: Request, db: Db, limit: Limit = 20, before_id: Before = None):
    settings = request.app.state.settings
    known_plan(db, settings, plan_id)
    rows, cursor = page(db, select(CollectionRun).where(CollectionRun.plan_id == plan_id,
                        *management.market(CollectionRun, settings)), CollectionRun, limit, before_id)
    return {"items": [management.run_summary(db, row) for row in rows], "next_before_id": cursor}


@router.get("/runs/{run_id}")
def run_detail(run_id: Identifier, request: Request, db: Db):
    row = db.scalar(select(CollectionRun).where(CollectionRun.id == run_id,
        *management.market(CollectionRun, request.app.state.settings)))
    if row is None:
        raise HTTPException(404, "collection_run_not_found")
    return management.run_summary(db, row, include_tasks=True)


@router.post("/tasks/{task_id}/cancel", dependencies=[Depends(require_write_token)])
def cancel(task_id: UUID, body: Input, request: Request):
    def change(session, settings):
        task, changed = management.cancel_task(session, settings, str(task_id))
        return {"changed": changed, "task": task_summary(task)}
    return perform(request, change)


@router.post("/tasks/{task_id}/retry", dependencies=[Depends(require_write_token)])
def retry(task_id: UUID, body: Input, request: Request):
    def change(session, settings):
        task, created = management.retry_task(session, settings, str(task_id))
        return {"created": created, "task": task_summary(task), "original_task_id": str(task_id)}
    return perform(request, change)


@router.get("/audit")
def operations(request: Request, db: Db, limit: Limit = 20, before_id: Before = None):
    rows, cursor = page(db, select(CollectionOperation).where(*management.market(CollectionOperation, request.app.state.settings)),
                        CollectionOperation, limit, before_id)
    return {"items": [{"id": str(row.id), "action": row.action, "target_type": row.target_type,
                        "target_id": row.target_id, "details": row.details, "created_at": row.created_at} for row in rows],
            "next_before_id": cursor}
