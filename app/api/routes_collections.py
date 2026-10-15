import json
import asyncio

from fastapi import HTTPException, Request


from app.api.async_router import AsyncAPIRouter


router = AsyncAPIRouter(prefix="/api/v1/collections", tags=["collections"])


@router.get("/current")
async def current_collection(request: Request):
    settings = request.app.state.settings
    try:
        content = await asyncio.to_thread((settings.collection_directory / "current.json").read_text, encoding="utf-8")
        state = json.loads(content)
    except FileNotFoundError:
        raise HTTPException(404, "collection_not_started") from None
    if state.get("platform") != settings.platform or state.get("region") != settings.region:
        raise HTTPException(404, "collection_not_in_this_market")
    return state
