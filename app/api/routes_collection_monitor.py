"""Read-only collection telemetry; short cache coalesces dashboard polling."""

import asyncio
import time
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.services.collection_heartbeat import read_runtime
from app.services.collection_monitor import WINDOWS, monitor_alerts, read_monitor
from app.storage.async_database import app_call


router = APIRouter(prefix="/api/v1/collections", tags=["collections"])


class MonitorCache:
    def __init__(self):
        self.values = {}
        self.pending = {}

    async def get(self, key, build):
        cached = self.values.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        task = self.pending.get(key)
        if task is None:
            async def load():
                try:
                    value = await build()
                    self.values[key] = (time.monotonic() + 10, value)
                    return value
                finally:
                    self.pending.pop(key, None)
            task = asyncio.create_task(load())
            self.pending[key] = task
        return await asyncio.shield(task)

    async def close(self):
        for task in self.pending.values():
            task.cancel()
        await asyncio.gather(*list(self.pending.values()), return_exceptions=True)
        self.pending.clear()


@router.get("/monitor")
async def monitor(request: Request, window_minutes: Annotated[int, Query(ge=15, le=1440)] = 60):
    if window_minutes not in WINDOWS:
        raise HTTPException(422, "invalid_monitor_window")
    settings = request.app.state.settings
    cache = getattr(request.app.state, "collection_monitor_cache", None)
    if cache is None:
        cache = request.app.state.collection_monitor_cache = MonitorCache()

    async def build():
        def query():
            with request.app.state.factory() as db:
                return read_monitor(db, settings, window_minutes)
        data, runtime = await asyncio.gather(app_call(request, query), read_runtime(settings))
        data["runtime"] = runtime
        data["alerts"] = monitor_alerts(data, scheduler_poll_seconds=settings.poll_seconds)
        return data

    data = await cache.get(window_minutes, build)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})
