import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from app.reports.cache import ReportCache, SerializedReport


def result(label):
    return SerializedReport(label, {"json": label.encode(), "html": label.encode(), "md": label.encode()})


def test_single_flight_shares_success_across_formats():
    cache, entered, release, barrier, calls = ReportCache(), Event(), Event(), Barrier(6), []
    def build():
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return result("original-generated-at")
    def request(index):
        barrier.wait(timeout=5)
        value = cache.get_or_build(("US", 24), build)
        return value.bodies[("json", "html", "md")[index % 3]]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(request, index) for index in range(6)]
        assert entered.wait(5)
        release.set()
        assert all(future.result() == b"original-generated-at" for future in futures)
    assert calls == [1]


def test_ttl_keeps_original_time_and_expiry_rebuilds():
    now, calls = [0.0], []
    cache = ReportCache(clock=lambda: now[0])
    def build():
        calls.append(1)
        return result(str(len(calls)))
    assert cache.get_or_build((24,), build).generated_at == "1"
    now[0] = 59
    assert cache.get_or_build((24,), build).generated_at == "1"
    now[0] = 60
    assert cache.get_or_build((24,), build).generated_at == "2"


def test_independent_caches_do_not_leak_between_apps_and_only_one_key_is_retained():
    first, second = ReportCache(), ReportCache()
    assert first.get_or_build((24,), lambda: result("app-a")).generated_at == "app-a"
    assert second.get_or_build((24,), lambda: result("app-b")).generated_at == "app-b"
    first.get_or_build((12,), lambda: result("other-window"))
    assert first.get_or_build((24,), lambda: result("rebuilt")).generated_at == "rebuilt"


def test_waiters_share_failure_and_a_later_request_can_recover():
    cache, entered, release, barrier, calls = ReportCache(), Event(), Event(), Barrier(6), []
    def fail():
        calls.append(1)
        entered.set()
        assert release.wait(5)
        raise ValueError("fixture-build-failed")
    def request():
        barrier.wait(timeout=5)
        with pytest.raises(ValueError, match="fixture-build-failed"):
            cache.get_or_build((24,), fail)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(request) for _ in range(6)]
        assert entered.wait(5)
        # Let the callers past their barrier attach to the same Future.
        time.sleep(0.05)
        release.set()
        for future in futures: future.result()
    assert calls == [1]
    assert cache.get_or_build((24,), lambda: result("recovered")).generated_at == "recovered"
