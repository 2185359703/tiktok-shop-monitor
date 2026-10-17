"""Best-effort, bounded process presence; never part of task ownership."""

import asyncio
import json
import re
import socket
from datetime import datetime, timezone
from uuid import uuid4

from redis.asyncio import Redis


INTERVAL_SECONDS = 10
STALE_SECONDS = 30
OFFLINE_SECONDS = 60
RETENTION_SECONDS = 600
MAX_COMPONENTS = 64
STATES = {"starting", "connecting", "running", "reconnecting", "degraded", "draining", "stopped", "disabled"}
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def instance_id():
    host = re.sub(r"[^A-Za-z0-9_.-]", "_", socket.gethostname())[:90]
    return f"{host}:{uuid4().hex[:12]}"


def prefix(settings):
    return f"{settings.queue_prefix}:{settings.platform}:{settings.region}:monitor"


def client_for(settings):
    return Redis.from_url(settings.redis_url, decode_responses=True,
                          socket_connect_timeout=.3, socket_timeout=.3,
                          retry_on_timeout=False)


def safe_state(state):
    """Whitelist telemetry fields before writing any process-provided values."""
    result = {"state": state.get("state") if state.get("state") in STATES else "starting"}
    for key in ("concurrency", "active_tasks", "published", "recovered"):
        value = state.get(key)
        if type(value) is int and 0 <= value <= 1_000_000:
            result[key] = value
    value = state.get("last_success_at")
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                result["last_success_at"] = parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    return result


class RuntimeHeartbeat:
    def __init__(self, settings, role, identifier, state, *, client=None):
        if role not in {"worker", "scheduler"} or not IDENTIFIER.fullmatch(identifier):
            raise ValueError("invalid_monitor_component")
        self.settings, self.role, self.identifier, self.state = settings, role, identifier, state
        self.client = client
        self.owns_client = client is None
        self.task = None
        self.stopped = False

    async def publish(self):
        if self.client is None:
            return False
        try:
            stamp = datetime.now(timezone.utc)
            values = self.state() if callable(self.state) else self.state
            payload = {"id": self.identifier, "role": self.role, "seen_at": stamp.isoformat(),
                       "enabled_task_types": [str(kind) for kind in self.settings.enabled_task_types],
                       **safe_state(values)}
            if self.stopped:
                payload["state"] = "stopped"
            key = prefix(self.settings)
            async with asyncio.timeout(1):
                async with self.client.pipeline(transaction=True) as pipe:
                    pipe.set(f"{key}:component:{self.identifier}", json.dumps(payload), ex=RETENTION_SECONDS)
                    pipe.zadd(f"{key}:components", {self.identifier: stamp.timestamp()})
                    pipe.zremrangebyscore(f"{key}:components", "-inf", stamp.timestamp() - RETENTION_SECONDS)
                    pipe.zremrangebyrank(f"{key}:components", 0, -(MAX_COMPONENTS + 1))
                    pipe.expire(f"{key}:components", RETENTION_SECONDS)
                    await pipe.execute()
            return True
        except Exception:
            # Monitoring failure must never cancel collection or leak URLs from exceptions.
            return False

    async def _run(self):
        while True:
            await self.publish()
            await asyncio.sleep(INTERVAL_SECONDS)

    def start(self):
        if self.client is None and self.settings.redis_url:
            self.client = client_for(self.settings)
        if self.client is not None:
            self.task = asyncio.create_task(self._run())
        return self

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.stopped = True
            await self.publish()
        if self.client is not None and self.owns_client:
            try:
                await self.client.aclose()
            except Exception:
                pass


async def read_runtime(settings, *, client=None, stamp=None):
    stamp = stamp or datetime.now(timezone.utc)
    empty = {"status": "not_configured", "observed_at": stamp.isoformat(),
             "components": [], "source_cooldown_seconds": None,
             "expected": {"worker": settings.monitor_expected_workers, "scheduler": settings.monitor_expected_schedulers},
             "ready_counts": {"worker": None, "scheduler": None}}
    if client is None and not settings.redis_url:
        return empty
    owns_client = client is None
    client = client or client_for(settings)
    key = prefix(settings)
    try:
        async with asyncio.timeout(1):
            identifiers = await client.zrevrangebyscore(f"{key}:components", "+inf",
                stamp.timestamp() - RETENTION_SECONDS, start=0, num=MAX_COMPONENTS)
            identifiers = [value for value in identifiers if isinstance(value, str) and IDENTIFIER.fullmatch(value)]
            async with client.pipeline(transaction=False) as pipe:
                for identifier in identifiers:
                    pipe.get(f"{key}:component:{identifier}")
                pipe.pttl(f"{settings.queue_prefix}:{settings.platform}:{settings.region}:pause")
                values = await pipe.execute()
        components = []
        for identifier, raw in zip(identifiers, values[:-1]):
            if not isinstance(raw, str) or len(raw) > 4096:
                continue
            try:
                data = json.loads(raw)
                if not isinstance(data, dict) or data.get("id") != identifier or data.get("role") not in {"worker", "scheduler"}:
                    continue
                seen = datetime.fromisoformat(data["seen_at"])
                if seen.tzinfo is None:
                    continue
                age = max(0, (stamp - seen).total_seconds())
                from app.models import TaskType
                enabled = data.get("enabled_task_types")
                enabled = [str(kind) for kind in TaskType if isinstance(enabled, list) and kind.value in enabled]
                components.append({"id": identifier, "role": data["role"], "seen_at": seen.isoformat(),
                    "age_seconds": round(age, 1), "status": "offline" if age >= OFFLINE_SECONDS or data.get("state") == "stopped" else
                    "stale" if age >= STALE_SECONDS else "online", "enabled_task_types": enabled,
                    **safe_state(data)})
            except (ValueError, TypeError, KeyError):
                continue
        for component in components:
            component["ready"] = component["status"] == "online" and component["state"] == "running"
        return {**empty, "status": "available", "components": components,
                "ready_counts": {role: sum(item["role"] == role and item["ready"] for item in components)
                                 for role in ("worker", "scheduler")},
                "source_cooldown_seconds": round(max(0, values[-1]) / 1000, 1)}
    except Exception:
        return {**empty, "status": "unavailable"}
    finally:
        if owns_client:
            try:
                await client.aclose()
            except Exception:
                pass
