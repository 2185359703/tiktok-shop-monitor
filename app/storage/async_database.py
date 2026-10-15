"""Native asyncio MySQL I/O with SQLAlchemy's supported greenlet ORM bridge.

run_sync is not a thread executor: every driver read/write yields to asyncio.
Pure query/transaction functions can therefore retain their tested ORM rules.
"""

import asyncio
import os

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.storage.database import configured_database_url, safe_database_url, validate_database_url


def make_async_engine(database_url=None):
    url = configured_database_url() if database_url is None else safe_database_url(database_url)
    validate_database_url(url)
    url = url.set(drivername="mysql+aiomysql").update_query_dict({"charset": "utf8mb4"})
    engine = create_async_engine(url, pool_pre_ping=True,
        pool_recycle=int(os.environ.get("DB_POOL_RECYCLE", "1800")),
        pool_size=int(os.environ.get("DB_POOL_SIZE", "10")),
        max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "10")),
        pool_timeout=float(os.environ.get("DB_POOL_TIMEOUT", "15")),
        connect_args={"connect_timeout": 10})

    @event.listens_for(engine.sync_engine, "connect")
    def options(connection, _):
        # SQLAlchemy adapts this event cursor to the native async driver.
        cursor = connection.cursor()
        try:
            cursor.execute("SET time_zone = '+00:00'")
            cursor.execute("SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
                           "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION'")
        finally:
            cursor.close()
    return engine


class AsyncDatabase:
    def __init__(self, engine):
        self.engine = engine
        self.factory = async_sessionmaker(engine, expire_on_commit=False)
        # For existing synchronous ORM functions inside run_sync ONLY. Calling
        # this factory outside the async bridge fails instead of blocking I/O.
        self.sync_factory = sessionmaker(engine.sync_engine, expire_on_commit=False)

    async def read(self, function, *args, **kwargs):
        async with self.factory() as session:
            return await session.run_sync(lambda db: function(db, *args, **kwargs))

    async def transaction(self, function, *args, **kwargs):
        async with self.factory.begin() as session:
            return await session.run_sync(lambda db: function(db, *args, **kwargs))

    async def call(self, function, *args, **kwargs):
        """Run a callable that opens its own sessions via sync_factory."""
        return await self.read(lambda _: function(*args, **kwargs))

    async def close(self):
        await self.engine.dispose()


async def app_call(request, function, /, *args, **kwargs):
    database = getattr(request.app.state, "database", None)
    if database is not None:
        return await database.call(function, *args, **kwargs)
    # Explicit injected sync engines/factories remain usable by existing tests
    # and embedding applications. Normal application startup always uses async.
    return await asyncio.to_thread(function, *args, **kwargs)
