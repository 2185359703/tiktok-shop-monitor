import json
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import create_app
from app.models import CrawlTask, ProductSnapshot, ResolutionStatus, ResourceType, SeedShopObservation, Shop, ShopSeedLink, TaskStatus, TaskType, VariantSnapshot
from app.services.collection_service import collection_progress, owned_task_queries
from app.services.seed_importer import SeedBatch, import_seed_batch, normalize_seed
from app.services.shop_discovery import ensure_product_discovery_task
from app.services.task_runtime import claim_task, database_now, finalize_task
from app.settings import Settings
from app.workers.contracts import HandlerContext
from app.workers.handlers import ProductDetailHandler
from app.crawlers.public_shop import PublicPageError
from app.queue.retry_policy import TaskFailure
from scripts.run_collection import save_state
from test_product_detail import SID, snapshot
from test_product_detail_storage import initial
from test_snapshot_pipeline import baseline


SHOP_URL = f"https://shop.tiktok.com/us/store/anrabess/{SID}"


def test_collection_task_scopes_avoid_dependent_cast_subqueries():
    from sqlalchemy.dialects.mysql import dialect
    queries = [str(query.compile(dialect=dialect(), compile_kwargs={"literal_binds": True}))
               for query in owned_task_queries([1], [2])]
    assert len(queries) == 3 and "JOIN crawl_tasks ON" in queries[2]
    assert "CAST(products.id AS CHAR(128))" in queries[2]
    assert all(" IN (SELECT" not in sql and " OR " not in sql for sql in queries)
    # The mode predicate reads a scalar JSON value, but progress queries must
    # never load large payloads or checkpoints into the Python result set.
    assert all("crawl_tasks.payload" not in sql.partition("\nFROM ")[0]
               and "crawl_tasks.checkpoint" not in sql.partition("\nFROM ")[0] for sql in queries)


def seed_batch():
    row = normalize_seed({"region": "US", "category": "women_clothing", "seed_type": "shop_url", "seed_value": SHOP_URL,
                          "shop_url": SHOP_URL, "source_url": SHOP_URL, "verification_status": "verified_storefront",
                          "observed_at": "2026-10-14"}, 2)
    return SeedBatch((row,), 1)


def scope_fixture(session):
    product, detail_task, _, _ = baseline(session)
    batch = seed_batch()
    import_seed_batch(session, batch)
    seed = session.scalar(select(SeedShopObservation))
    seed.resolution_status = ResolutionStatus.RESOLVED
    shop = session.get(Shop, product.shop_id)
    shop.canonical_url = SHOP_URL
    session.add(ShopSeedLink(seed_id=seed.id, shop_id=shop.id, platform="tiktok_shop", region="US",
                            resolved_at=database_now(session), resolution_method="test_fixture", evidence_url=SHOP_URL))
    identity = session.scalar(select(CrawlTask).where(CrawlTask.task_type == TaskType.SHOP_DISCOVERY))
    identity.status = detail_task.status = TaskStatus.SUCCEEDED
    ensure_product_discovery_task(session, shop, SHOP_URL, database_now(session))
    page = session.scalar(select(CrawlTask).where(CrawlTask.task_type == TaskType.PRODUCT_DISCOVERY))
    page.status, page.checkpoint = TaskStatus.SUCCEEDED, {"complete": True, "stop_reason": "source_exhausted"}
    session.flush()
    return batch, page


def test_collection_waits_for_baseline_comparison_and_counts_its_fixed_scope(session):
    batch, _ = scope_fixture(session)
    state = collection_progress(session, batch)
    assert state["source_complete_shops"] == state["products"] == state["details_completed"] == state["baseline_products"] == 1
    assert state["variants"] == 2 and state["active_tasks"] == 1 and not state["full_coverage"]
    comparison = session.scalar(select(CrawlTask).where(CrawlTask.task_type == TaskType.CHANGE_DETECTION))
    comparison.status = TaskStatus.SUCCEEDED
    session.flush()
    assert collection_progress(session, batch)["full_coverage"]


def test_collection_join_excludes_unowned_products_and_other_markets(session):
    batch, _ = scope_fixture(session)
    comparison = session.scalar(select(CrawlTask).where(CrawlTask.task_type == TaskType.CHANGE_DETECTION))
    comparison.status = TaskStatus.SUCCEEDED
    session.add_all([
        CrawlTask(platform="tiktok_shop", region="US", task_type=TaskType.PRODUCT_DETAIL,
                  resource_type=ResourceType.PRODUCT, resource_id="99999999", status=TaskStatus.DEAD),
        CrawlTask(platform="tiktok_shop", region="GB", task_type=TaskType.PRODUCT_DETAIL,
                  resource_type=ResourceType.PRODUCT, resource_id=comparison.resource_id, status=TaskStatus.DEAD),
    ])
    session.flush()
    state = collection_progress(session, batch)
    assert state["failed_tasks"] == 0 and state["full_coverage"]


def test_image_backfill_is_accounted_separately_from_initial_collection(session):
    batch, _ = scope_fixture(session)
    comparison = session.scalar(select(CrawlTask).where(CrawlTask.task_type == TaskType.CHANGE_DETECTION))
    comparison.status = TaskStatus.SUCCEEDED
    session.add(CrawlTask(platform="tiktok_shop", region="US", task_type=TaskType.PRODUCT_DETAIL,
                          resource_type=ResourceType.PRODUCT, resource_id=comparison.resource_id,
                          payload={"capture_mode": "images_only"}, status=TaskStatus.DEAD))
    session.flush()
    state = collection_progress(session, batch)
    assert state["full_coverage"] and state["failed_tasks"] == 0


def test_empty_queue_at_a_page_budget_is_partial_coverage(session):
    batch, page = scope_fixture(session)
    for task in session.scalars(select(CrawlTask)):
        task.status = TaskStatus.SUCCEEDED
    page.checkpoint = {"complete": False, "stop_reason": "page_budget"}
    session.flush()
    state = collection_progress(session, batch)
    assert state["active_tasks"] == 0 and state["source_complete_shops"] == 0
    assert not state["full_coverage"] and state["shops"][0]["source_status"] == "page_budget"


def test_failed_identity_is_accounted_as_failed_not_a_zero_product_store(session):
    batch = seed_batch()
    import_seed_batch(session, batch)
    task = session.scalar(select(CrawlTask))
    task.status, task.last_error = TaskStatus.DEAD, "公开来源身份未确认"
    session.flush()
    state = collection_progress(session, batch)
    assert state["failed_tasks"] == 1 and state["shops"][0]["source_status"] == "identity_failed"
    assert not state["full_coverage"] and state["failures"][0]["count"] == 1


def test_missing_seed_observations_cannot_be_called_a_complete_empty_collection(session):
    state = collection_progress(session, seed_batch())
    assert state["target_shops"] == 1 and state["seed_observations"] == 0 and not state["full_coverage"]


def test_detail_opt_in_saves_baseline_in_the_same_task_without_a_second_request(session, monkeypatch, tmp_path):
    _, task = initial(session)
    response, calls = snapshot(), []
    config = replace(Settings(), enabled_task_types=(TaskType.PRODUCT_DETAIL,), baseline_on_detail=True, evidence_directory=tmp_path)
    def read_detail(*args):
        calls.append(1)
        return response
    monkeypatch.setattr("app.workers.handlers.TikTokShopAdapter.fetch_product_detail", read_detail)
    lease = claim_task(session, task.task_id, task.task_type, "full-collection-test", config)
    result = ProductDetailHandler().prepare(lease, HandlerContext(config, None, Event()))
    assert finalize_task(session, lease, config, result=result) == TaskStatus.SUCCEEDED
    assert calls == [1]
    assert session.scalar(select(func.count()).select_from(ProductSnapshot)) == 1
    assert session.scalar(select(func.count()).select_from(VariantSnapshot)) == 2
    assert task.checkpoint["snapshots_written"] == 1 and task.checkpoint["change_task_created"]


def test_collection_api_reads_atomic_state_and_is_market_scoped(mysql_engine, tmp_path):
    config = replace(Settings(), collection_directory=tmp_path)
    with TestClient(create_app(config, mysql_engine)) as client:
        assert client.get("/api/v1/collections/current").status_code == 404
        state = {"platform": "tiktok_shop", "region": "US", "job_id": "batch", "products": 123, "status": "running"}
        save_state(tmp_path, state)
        assert client.get("/api/v1/collections/current").json() == state
        assert json.loads((tmp_path / "current.json").read_text(encoding="utf-8")) == state
        save_state(tmp_path, {**state, "region": "GB"})
        assert client.get("/api/v1/collections/current").status_code == 404


def test_server_profile_is_explicit_while_local_collection_stays_off():
    local, server = Settings(), Settings.load(Path("configs/server.toml"))
    assert not local.baseline_on_detail and not local.enabled_task_types
    assert server.baseline_on_detail and server.product_discovery_max_pages == 500
    assert not server.periodic_snapshots_enabled


@pytest.mark.parametrize("status,global_pause", [(200, False), (403, True), (429, True)])
def test_resource_document_cooldown_preserves_global_network_denial_pause(session, monkeypatch, tmp_path, status, global_pause):
    _, task = initial(session)
    config = replace(Settings(), enabled_task_types=(TaskType.PRODUCT_DETAIL,), resource_document_cooldown=True, evidence_directory=tmp_path)
    calls = []
    class Gate:
        def pause(self): calls.append(1)
    def denied(*args): raise PublicPageError("access_limited", "公开资源受限", status=status)
    monkeypatch.setattr("app.workers.handlers.TikTokShopAdapter.fetch_product_detail", denied)
    lease = claim_task(session, task.task_id, task.task_type, "scope-test", config)
    with pytest.raises(TaskFailure) as error:
        ProductDetailHandler().prepare(lease, HandlerContext(config, Gate(), Event()))
    assert bool(calls) is global_pause
    assert error.value.failure.retryable and error.value.failure.delay_seconds == 900
