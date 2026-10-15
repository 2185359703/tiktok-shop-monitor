"""Exercise the profile handler against fake HTTP and the real lease boundary."""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event

import httpx
import pytest
from sqlalchemy import func, select

from app.crawlers.shop_profile import PARSER_VERSION, ShopProfileClient
from app.models import CrawlAttempt, CrawlTask, Shop, ShopProfileSnapshot, TaskStatus, TaskType
from app.queue.retry_policy import TaskFailure
from app.services.shop_profile import ensure_shop_profile_task, shop_profile_window
from app.services.task_runtime import LeaseLost, claim_task, database_now, finalize_task
from app.settings import Settings
from app.workers.contracts import HandlerContext
from app.workers.handlers import ShopProfileHandler, handler_registry, validate_enabled
from test_shop_profile import SHOP_ID, URL, html, profile_info, router


CONFIG = Settings(enabled_task_types=(TaskType.SHOP_PROFILE,))


class Gate:
    def __init__(self):
        self.requests = 0
        self.pauses = 0

    def wait_turn(self, stop):
        assert not stop.is_set()
        self.requests += 1

    def pause(self):
        self.pauses += 1


def initial_task(session):
    shop = Shop(region="US", platform_shop_id=SHOP_ID, canonical_name="Before profile", canonical_url=URL)
    session.add(shop)
    session.flush()
    now = database_now(session)
    ensure_shop_profile_task(session, shop, shop_profile_window(now, 86400))
    task = session.scalar(select(CrawlTask).where(CrawlTask.task_type == TaskType.SHOP_PROFILE))
    task.available_at = now - timedelta(seconds=1)
    session.flush()
    lease = claim_task(session, task.task_id, task.task_type, "profile-handler-test", CONFIG)
    assert lease is not None
    return shop, task, lease


def fake_http(monkeypatch, response):
    requests = []
    def respond(request):
        requests.append(request)
        return response
    def client(**kwargs):
        return ShopProfileClient(**kwargs, transport=httpx.MockTransport(respond))
    monkeypatch.setattr("app.adapters.tiktok_shop.ShopProfileClient", client)
    return requests


def test_handler_prepares_outside_sql_then_atomically_finalizes_profile(session, monkeypatch, tmp_path):
    shop, task, lease = initial_task(session)
    gate = Gate()
    requests = fake_http(monkeypatch, httpx.Response(200, text=html(router())))
    context = HandlerContext(replace(CONFIG, evidence_directory=tmp_path), gate, Event())
    result = ShopProfileHandler().prepare(lease, context)
    assert session.scalar(select(func.count()).select_from(ShopProfileSnapshot)) == 0
    assert task.status == TaskStatus.RUNNING and shop.follower_count is None
    assert len(requests) == gate.requests == 1 and gate.pauses == 0
    assert finalize_task(session, lease, CONFIG, result=result) == TaskStatus.SUCCEEDED
    observed = session.scalar(select(ShopProfileSnapshot))
    attempt = session.scalar(select(CrawlAttempt).where(CrawlAttempt.task_id == task.id))
    assert observed.task_id == task.id and observed.shop_id == shop.id
    assert shop.follower_count == observed.follower_count == 34872
    assert shop.video_count == observed.video_count == 1391
    assert shop.sales_count == observed.sales_count == 498223
    assert shop.profile_captured_at == observed.captured_at
    assert task.checkpoint["snapshots_written"] == 1 and task.checkpoint["observation_status"] == "complete"
    assert task.lease_token is None and attempt.succeeded is True
    assert attempt.response_hash == observed.response_hash and attempt.response_status == 200
    assert json.loads(Path(attempt.evidence_path).read_text(encoding="utf-8"))["parser_version"] == PARSER_VERSION
    with pytest.raises(LeaseLost):
        finalize_task(session, lease, CONFIG, result=result)
    assert session.scalar(select(func.count()).select_from(ShopProfileSnapshot)) == 1


def test_missing_optional_header_fields_finish_as_partial_without_zero_invention(session, monkeypatch, tmp_path):
    shop, task, lease = initial_task(session)
    info = profile_info()
    info.pop("video_count")
    info.pop("format_video_count")
    fake_http(monkeypatch, httpx.Response(200, text=html(router(info))))
    result = ShopProfileHandler().prepare(lease, HandlerContext(replace(CONFIG, evidence_directory=tmp_path), Gate(), Event()))
    finalize_task(session, lease, CONFIG, result=result)
    observed = session.scalar(select(ShopProfileSnapshot))
    assert task.status == TaskStatus.SUCCEEDED and task.checkpoint["observation_status"] == "partial"
    assert observed.video_count is None and shop.video_count is None
    assert observed.field_state["video_count"]["state"] == "not_provided"


def test_bad_identity_failure_keeps_previous_current_profile_and_records_no_snapshot(session, monkeypatch, tmp_path):
    shop, task, lease = initial_task(session)
    previous = database_now(session) - timedelta(days=1)
    shop.follower_count, shop.sales_count, shop.video_count = 12, 34, 56
    shop.profile_captured_at = previous
    session.flush()
    info = profile_info()
    info["seller_id"] = "111"
    fake_http(monkeypatch, httpx.Response(200, text=html(router(info))))
    context = HandlerContext(replace(CONFIG, evidence_directory=tmp_path), Gate(), Event())
    with pytest.raises(TaskFailure) as error:
        ShopProfileHandler().prepare(lease, context)
    assert error.value.failure.code == "store_identity_mismatch"
    assert finalize_task(session, lease, CONFIG, failure=error.value.failure) == TaskStatus.DEAD
    assert shop.follower_count == 12 and shop.sales_count == 34 and shop.video_count == 56
    assert shop.profile_captured_at == previous
    assert session.scalar(select(func.count()).select_from(ShopProfileSnapshot)) == 0
    attempt = session.scalar(select(CrawlAttempt).where(CrawlAttempt.task_id == task.id))
    assert attempt.succeeded is False and attempt.error_type == "store_identity_mismatch"
    assert attempt.response_status == 200 and len(attempt.response_hash) == 64
    evidence = json.loads(Path(attempt.evidence_path).read_text(encoding="utf-8"))
    assert evidence["parser_version"] == PARSER_VERSION


@pytest.mark.parametrize("status,cooldown,expected_pauses", [(403, False, 1), (429, True, 1), (200, True, 0)])
def test_access_limits_record_retry_without_fake_observations(session, monkeypatch, tmp_path, status, cooldown, expected_pauses):
    shop, task, lease = initial_task(session)
    gate = Gate()
    config = replace(CONFIG, evidence_directory=tmp_path, resource_document_cooldown=cooldown)
    requests = fake_http(monkeypatch, httpx.Response(status, text="<title>Security Check</title>"))
    with pytest.raises(TaskFailure) as error:
        ShopProfileHandler().prepare(lease, HandlerContext(config, gate, Event()))
    assert error.value.failure.code == "access_limited" and error.value.failure.retryable
    assert finalize_task(session, lease, config, failure=error.value.failure) == TaskStatus.RETRY_WAITING
    assert len(requests) == gate.requests == 1 and gate.pauses == expected_pauses
    assert task.scheduled_for == shop_profile_window(database_now(session), 86400)
    assert task.available_at > database_now(session) and task.attempt_count == 1
    assert shop.profile_captured_at is None and session.scalar(select(func.count()).select_from(ShopProfileSnapshot)) == 0


def test_invalid_payload_is_rejected_before_the_first_http_request(session, monkeypatch, tmp_path):
    _, _, lease = initial_task(session)
    requests = fake_http(monkeypatch, httpx.Response(200, text=html(router())))
    bad = replace(lease, payload={**lease.payload, "platform_shop_id": "111"})
    with pytest.raises(TaskFailure) as error:
        ShopProfileHandler().prepare(bad, HandlerContext(replace(CONFIG, evidence_directory=tmp_path), Gate(), Event()))
    assert error.value.failure.code == "invalid_shop_profile_task" and requests == []


def test_default_profile_scheduling_remains_off_and_handler_is_registered():
    settings = Settings()
    assert not settings.shop_profiles_enabled and settings.shop_profile_shop_ids == ()
    assert settings.shop_profile_interval_seconds == 86400
    registry = handler_registry()
    assert isinstance(registry[TaskType.SHOP_PROFILE], ShopProfileHandler)
    validate_enabled(CONFIG, registry)
    enabled = replace(CONFIG, shop_profiles_enabled=True, shop_profile_shop_ids=(SHOP_ID,))
    assert enabled.shop_profiles_enabled


@pytest.mark.parametrize("changes", [
    {"shop_profiles_enabled": "true"},
    {"shop_profile_interval_seconds": True},
    {"shop_profile_interval_seconds": 3599},
    {"shop_profile_interval_seconds": 604801},
    {"shop_profile_jitter_seconds": -1},
    {"shop_profile_jitter_seconds": 86400},
    {"shop_profile_jitter_seconds": False},
    {"shop_profile_shop_ids": (SHOP_ID, SHOP_ID)},
    {"shop_profile_shop_ids": (int(SHOP_ID),)},
    {"shop_profile_shop_ids": ("invalid",)},
    {"shop_profile_shop_ids": "123"},
    {"shop_profiles_enabled": True},
    {"shop_profiles_enabled": True, "shop_profile_shop_ids": (SHOP_ID,), "enabled_task_types": ()},
])
def test_profile_scheduling_requires_valid_explicit_settings(changes):
    with pytest.raises(ValueError):
        replace(CONFIG, **changes)
