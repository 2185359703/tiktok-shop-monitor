import json

from fastapi import APIRouter, HTTPException, Request


router = APIRouter(prefix="/api/v1/collections", tags=["collections"])


@router.get("/current")
def current_collection(request: Request):
    settings = request.app.state.settings
    try:
        state = json.loads((settings.collection_directory / "current.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HTTPException(404, "collection_not_started") from None
    if state.get("platform") != settings.platform or state.get("region") != settings.region:
        raise HTTPException(404, "collection_not_in_this_market")
    return state
