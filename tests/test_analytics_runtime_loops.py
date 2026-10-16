"""Long statistics must not stop incremental projection or lightweight counters."""

import asyncio
from types import SimpleNamespace
import pytest

from app.services.analytics_refresh import HEAVY_KINDS, LIGHT_KINDS, SUMMARY_KINDS
from app.settings import Settings
from scripts import run_analytics


def test_blocked_heavy_refresh_does_not_hold_the_projection_loop(monkeypatch):
    async def scenario():
        stop, heavy_started, release_heavy = asyncio.Event(), asyncio.Event(), asyncio.Event()
        processed, counters = [], []
        closed = []

        class Engine:
            def execution_options(self, **options):
                return self

        class Database:
            def __init__(self, engine):
                pass

            async def read(self, *args, **kwargs):
                return True

            async def transaction(self, *args, **kwargs):
                await asyncio.sleep(0)

            async def close(self):
                closed.append(True)

        class Runner:
            def __init__(self, *args):
                pass

            async def run_once(self, *, only_kinds=None, slot_name=None, **kwargs):
                if only_kinds == HEAVY_KINDS:
                    heavy_started.set()
                    await release_heavy.wait()
                elif only_kinds == LIGHT_KINDS:
                    counters.append(1)
                elif only_kinds == SUMMARY_KINDS:
                    counters.append(2)
                else:
                    assert only_kinds == ("product_projection", "event_projection")
                    await heavy_started.wait()
                    assert not release_heavy.is_set()
                    processed.append(1)
                    if len(processed) == 3:
                        stop.set()
                        release_heavy.set()
                return True

        async def pause(*args):
            await asyncio.sleep(0)

        monkeypatch.setattr(run_analytics, "make_async_engine", lambda url: Engine())
        monkeypatch.setattr(run_analytics, "AsyncDatabase", Database)
        monkeypatch.setattr(run_analytics, "AsyncAnalyticsRunner", Runner)
        monkeypatch.setattr(run_analytics, "stop_signal", lambda: stop)
        monkeypatch.setattr(run_analytics, "pause_until_stop", pause)
        monkeypatch.setattr(run_analytics, "enabled", lambda: True)
        await asyncio.wait_for(run_analytics.run(Settings(analytics_concurrency=1),
            SimpleNamespace(status=False, once=False)), timeout=2)
        assert len(processed) == 3 and counters and closed == [True]

    asyncio.run(scenario())


@pytest.mark.parametrize("role,kinds", [("projections", {("product_projection", "event_projection"), LIGHT_KINDS}),
    ("summaries", {SUMMARY_KINDS}), ("heavy", {HEAVY_KINDS})])
def test_process_roles_claim_only_their_own_queues(monkeypatch, role, kinds):
    async def scenario():
        stop, seen = asyncio.Event(), set()
        class Engine:
            def execution_options(self, **options): return self
        class Database:
            def __init__(self, *args): pass
            async def read(self, *args): return True
            async def transaction(self, *args): await asyncio.sleep(0)
            async def close(self): pass
        class Runner:
            def __init__(self, *args): pass
            async def run_once(self, *, only_kinds, **kwargs):
                assert only_kinds in kinds
                seen.add(only_kinds)
                if seen == kinds: stop.set()
                return True
        async def pause(*args): await asyncio.sleep(0)
        monkeypatch.setattr(run_analytics, "make_async_engine", lambda url: Engine())
        monkeypatch.setattr(run_analytics, "AsyncDatabase", Database)
        monkeypatch.setattr(run_analytics, "AsyncAnalyticsRunner", Runner)
        monkeypatch.setattr(run_analytics, "stop_signal", lambda: stop)
        monkeypatch.setattr(run_analytics, "pause_until_stop", pause)
        monkeypatch.setattr(run_analytics, "enabled", lambda: True)
        await asyncio.wait_for(run_analytics.run(Settings(analytics_concurrency=1),
            SimpleNamespace(status=False, once=False, role=role)), timeout=2)
        assert seen == kinds
    asyncio.run(scenario())
