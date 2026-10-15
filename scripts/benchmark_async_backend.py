"""Read-only local throughput and responsiveness probe; never enqueues tasks."""

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import text

from app.main import create_app
from app.settings import Settings
from app.storage.async_database import AsyncDatabase, make_async_engine


async def measure(config):
    database = AsyncDatabase(make_async_engine(config.database_url))
    result = {"observed_at": datetime.now(timezone.utc).isoformat(), "driver": database.engine.dialect.driver,
              "worker_concurrency": config.worker_concurrency, "shared_request_interval_seconds": config.minimum_interval_seconds,
              "scope": "local MySQL waits and existing API data; no collection or writes"}
    try:
        await asyncio.gather(*(database.read(lambda db: db.scalar(text("SELECT 1"))) for _ in range(8)))
        async def probe():
            return await database.read(lambda db: db.scalar(text("SELECT SLEEP(0.1)")))
        start = time.perf_counter()
        for _ in range(8):
            await probe()
        sequential = time.perf_counter() - start
        start = time.perf_counter()
        await asyncio.gather(*(probe() for _ in range(8)))
        concurrent = time.perf_counter() - start
        result["database_wait_benchmark"] = {"operations": 8, "wait_per_operation_ms": 100,
            "sequential_ms": round(sequential * 1000, 1), "concurrent_ms": round(concurrent * 1000, 1),
            "overlap_speedup": round(sequential / concurrent, 2)}
        app = create_app(settings=config, engine=database.engine)
        paths = ("/health/ready", "/api/v1/overview", "/api/v1/products?limit=5", "/api/v1/shops?limit=5",
                 "/api/v1/analytics/products?page_size=5", "/api/v1/analytics/workbench", "/api/v1/reports/current", "/api/v2/ui/bootstrap")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
                async def request(path):
                    start = time.perf_counter()
                    response = await client.get(path)
                    return {"path": path, "status": response.status_code,
                            "elapsed_ms": round((time.perf_counter() - start) * 1000, 1), "bytes": len(response.content)}
                ticks, stop = [], asyncio.Event()
                async def heartbeat():
                    previous = time.perf_counter()
                    while not stop.is_set():
                        await asyncio.sleep(.01)
                        current = time.perf_counter()
                        ticks.append(max(0, current - previous - .01) * 1000)
                        previous = current
                timer = asyncio.create_task(heartbeat())
                start = time.perf_counter()
                try:
                    result["api"] = await asyncio.gather(*(request(path) for path in paths))
                    result["api_batch_ms"] = round((time.perf_counter() - start) * 1000, 1)
                finally:
                    stop.set()
                    await timer
                result["event_loop_max_lag_ms"] = round(max(ticks, default=0), 1)
        return result
    finally:
        await database.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/backend-async/benchmark.json"))
    args = parser.parse_args(argv)
    result = asyncio.run(measure(Settings.load()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if all(item["status"] == 200 for item in result["api"]) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__}))
        raise SystemExit(1)
