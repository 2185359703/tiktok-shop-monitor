"""Register coroutine routes while preserving existing API and ORM contracts."""

import inspect
from functools import wraps

from fastapi import APIRouter, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.storage.async_database import app_call


class AsyncAPIRouter(APIRouter):
    def add_api_route(self, path, endpoint, **kwargs):
        if not inspect.iscoroutinefunction(endpoint):
            original = endpoint

            @wraps(original)
            async def endpoint(*args, **values):
                sessions = [(key, value) for key, value in values.items() if isinstance(value, AsyncSession)]
                if sessions:
                    key, session = sessions[0]
                    return await session.run_sync(lambda db: original(*args, **{**values, key: db}))
                request = next((v for v in (*args, *values.values()) if isinstance(v, Request)), None)
                if request is not None:
                    return await app_call(request, original, *args, **values)
                return await run_in_threadpool(original, *args, **values)

            endpoint.__signature__ = inspect.signature(original, eval_str=True)
        return super().add_api_route(path, endpoint, **kwargs)
