"""Read incident history and acknowledge known incidents without changing collection."""

from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict

from app.api.async_router import AsyncAPIRouter
from app.api.routes import Db, require_write_token
from app.services import collection_alerts as alerts
from app.storage.database import session_scope


router = AsyncAPIRouter(prefix="/api/v1/collections/alerts", tags=["collection-alerts"])
Identifier = Annotated[int, Path(ge=1, le=2 ** 64 - 1)]
Limit = Annotated[int, Query(ge=1, le=50)]
Before = Annotated[int | None, Query(ge=1, le=2 ** 64 - 1)]
Status = Literal["active", "open", "acknowledged", "resolved", "all"]


class AcknowledgeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.get("")
def list_alerts(request: Request, db: Db, status: Status = "active", limit: Limit = 20, before_id: Before = None):
    return alerts.list_alerts(db, request.app.state.settings, status=status, limit=limit, before_id=before_id)


@router.get("/{alert_id}/events")
def list_events(alert_id: Identifier, request: Request, db: Db, limit: Limit = 20, before_id: Before = None):
    try:
        return alerts.list_events(db, request.app.state.settings, alert_id, limit=limit, before_id=before_id)
    except alerts.AlertError as error:
        raise HTTPException(error.status, error.code) from None


@router.post("/{alert_id}/ack", dependencies=[Depends(require_write_token)])
def acknowledge(alert_id: Identifier, body: AcknowledgeInput, request: Request):
    try:
        with session_scope(request.app.state.factory) as db:
            return alerts.acknowledge(db, request.app.state.settings, alert_id)
    except alerts.AlertError as error:
        raise HTTPException(error.status, error.code) from None
