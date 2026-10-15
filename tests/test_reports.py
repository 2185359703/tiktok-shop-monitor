import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from threading import Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.main import create_app
from app.models import Product, ProductVariant, Shop, StockStatus, TaskStatus, TaskType
from app.queue.retry_policy import TaskFailure
from app.reports.summary import build_monitoring_report, percentage, price_statistics
from app.reports.exporter import export_report, render_html, render_markdown
from app.services.product_detail import persist_product_detail
from app.services.report_service import ensure_report_task
from app.services.snapshot_service import ensure_snapshot_task, persist_snapshot, utc_window
from app.services.task_runtime import claim_task, database_now, finalize_task
from app.settings import Settings
from app.workers.contracts import HandlerContext
from app.workers.handlers import ReportGenerationHandler
from test_snapshot_pipeline import baseline, detect, next_observation
from test_product_detail import snapshot
from test_product_detail_storage import initial


def test_decimal_statistics_do_not_count_null_as_zero():
    assert price_statistics([None, Decimal("0.00"), Decimal("10.01")])["median"] == "5.01"
    assert price_statistics([None])["median"] is None
    assert percentage(1, 3) == "33.33" and percentage(0, 0) is None


def test_report_counts_latest_observation_once_and_excludes_future_fields(session):
    product, _, response, _ = baseline(session)
    future = response.captured_at + timedelta(seconds=60)
    task, _ = ensure_snapshot_task(session, product, future)
    changed = replace(response, captured_at=future, started_at=future,
                      detail=replace(response.detail, price_min=Decimal("1.00"), price_max=Decimal("1.00"), title="Later title"))
    persist_snapshot(session, task, changed)
    report = build_monitoring_report(session, Settings())
    assert report["summary"]["products"] == 1 and report["summary"]["observed_skus"] == 2
    assert report["products"][0]["price_min"] == "81.89"
    assert report["products"][0]["title"] != "Later title"
    assert report["summary"]["starting_price_stats"]["median"] == "81.89"


def test_unknown_sku_stock_is_not_in_the_stockout_denominator(session):
    product, task = initial(session)
    response = snapshot()
    response = replace(response, detail=replace(response.detail,
                        variants=(replace(response.detail.variants[0], stock_status=StockStatus.UNKNOWN, available_quantity=None), response.detail.variants[1])))
    persist_product_detail(session, task, response)
    persist_snapshot(session, task, response, update_current=False)
    report = build_monitoring_report(session, Settings())
    assert report["summary"]["observed_skus"] == 2 and report["summary"]["known_stock_skus"] == 1
    assert report["summary"]["sku_stockout_pct"] == "100.00"


def test_report_pages_products_and_never_loads_full_snapshot_json_or_sku_objects(session, monkeypatch):
    product, _, _, _ = baseline(session)
    session.add(Product(region="US", shop_id=product.shop_id, platform_product_id="444",
                        product_url="https://example.com", title="Discovery-only product"))
    session.flush()
    session.expunge_all()
    monkeypatch.setattr("app.reports.summary.PRODUCT_BATCH_SIZE", 1)
    statements = []
    def record_sql(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    connection = session.connection()
    event.listen(connection, "before_cursor_execute", record_sql)
    try:
        report = build_monitoring_report(session, Settings())
    finally:
        event.remove(connection, "before_cursor_execute", record_sql)
    assert report["summary"]["products"] == 2 and report["summary"]["observed_skus"] == 2
    assert report["summary"]["historical_detail_products"] == 1
    assert not any("product_snapshots.raw_payload AS" in sql or "variant_snapshots.id," in sql for sql in statements)


def test_report_capacity_guard_is_explicit_instead_of_truncating(session, monkeypatch):
    baseline(session)
    monkeypatch.setattr("app.reports.summary.MAX_PRODUCTS", 0)
    with pytest.raises(ValueError, match="不会截断冒充全量"):
        build_monitoring_report(session, Settings())


def test_report_does_not_merge_other_markets_or_price_contexts(session):
    product, _, _, _ = baseline(session)
    shop = Shop(region="GB", platform_shop_id="999", canonical_name="Outside market")
    session.add(shop); session.flush()
    session.add(Product(region="GB", shop_id=shop.id, platform_product_id="555", product_url="https://example.com", currency="GBP"))
    session.add(Product(region="US", shop_id=product.shop_id, platform_product_id="444", product_url="https://example.com",
                        pricing_context="member_discount", price_min=Decimal("1.00")))
    session.flush()
    report = build_monitoring_report(session, Settings())
    assert report["summary"]["products"] == 1
    assert report["summary"]["excluded_price_context_products"] == 1
    assert all(item["platform_product_id"] != "555" for item in report["products"])


def test_starting_prices_are_not_labelled_exact_and_unknown_category_remains_unknown(session):
    product, task = initial(session)
    product.price_min, product.price_max, product.original_category = Decimal("27.99"), None, None
    session.flush()
    report = build_monitoring_report(session, Settings())
    assert report["products"][0]["price_kind"] == "starting_price"
    assert report["categories"][0]["category"] == "unknown"
    assert report["summary"]["observed_skus"] == 0 and report["summary"]["sku_stockout_pct"] is None


def test_reports_escape_merchant_markup_and_export_identical_input_idempotently(session, tmp_path):
    product, _, _, _ = baseline(session)
    report = build_monitoring_report(session, Settings())
    report["products"][0]["title"] = '</script><script>window.injected=true</script> A|B'
    report["shops"][0]["name"] = '<img onerror="window.injected=true">|name'
    rendered = render_html(report)
    assert '</script><script>window.injected=true' not in rendered
    assert "\\u003c/script\\u003e" in rendered
    markdown = render_markdown(report)
    assert '<img onerror=' not in markdown and "\\|name" in markdown
    first = export_report(report, tmp_path)
    again = export_report(report, tmp_path)
    assert first == again and len(list(tmp_path.glob("us-clothing-*"))) == 3


def test_streamed_report_bytes_preserve_unicode_escaping_and_content_hash(session, tmp_path):
    from app.reports.cache import serialize_report
    baseline(session)
    report = build_monitoring_report(session, Settings())
    report["products"][0]["title"] = "Emoji 👗 and 中文 </script> & <script>"
    artifact = export_report(report, tmp_path)
    expected = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    assert open(artifact["files"]["json"], "rb").read() == expected
    assert artifact["report_hash"] == sha256(expected).hexdigest()
    cached = serialize_report(report)
    assert json.loads(cached.bodies["json"])["products"][0]["title"] == report["products"][0]["title"]
    assert cached.bodies["html"] == render_html(report).encode("utf-8")
    assert b"</script> & <script>" not in cached.bodies["html"]


def test_failed_stream_cleans_only_its_own_temporary_file(tmp_path):
    from app.reports.exporter import stage_report_file
    existing = tmp_path / "keep.json"
    existing.write_bytes(b"existing-artifact")
    def fails():
        yield "partial"
        raise OSError("fixture-write-interrupted")
    with pytest.raises(OSError): stage_report_file(tmp_path, fails())
    assert existing.read_bytes() == b"existing-artifact"
    assert not list(tmp_path.glob(".report-*.tmp"))


def test_invalid_cutoff_and_future_window_are_rejected(session):
    with pytest.raises(ValueError): build_monitoring_report(session, Settings(), as_of=database_now(session).replace(tzinfo=None))
    with pytest.raises(ValueError): build_monitoring_report(session, Settings(), as_of=database_now(session) + timedelta(days=1))
    with pytest.raises(ValueError): ensure_report_task(session, Settings(), database_now(session) + timedelta(days=1))


def test_report_task_is_idempotent_and_runs_without_a_site_adapter(session, tmp_path):
    baseline(session)
    config = replace(Settings(), enabled_task_types=(TaskType.REPORT_GENERATION,), report_directory=tmp_path)
    window = utc_window(database_now(session), 3600)
    task, created = ensure_report_task(session, config, window)
    same, duplicated = ensure_report_task(session, config, window)
    assert created and not duplicated and same.task_id == task.task_id
    lease = claim_task(session, task.task_id, task.task_type, "report-test", config)
    class NoWebsite:
        def wait_turn(self, stop): raise AssertionError("Report must not fetch a website")
    result = ReportGenerationHandler().prepare(lease, HandlerContext(config, NoWebsite(), Event(),
                                                                    session_factory=lambda: nullcontext(session)))
    assert finalize_task(session, lease, config, result=result) == TaskStatus.SUCCEEDED
    content = json.loads(open(task.checkpoint["files"]["json"], encoding="utf-8").read())
    assert content["summary"]["products"] == 1 and task.checkpoint["report_products"] == 1


def test_report_jobs_do_not_reuse_a_different_price_context(session):
    config = Settings()
    window = utc_window(database_now(session), 3600)
    public, _ = ensure_report_task(session, config, window)
    member, _ = ensure_report_task(session, replace(config, pricing_context="member_discount"), window)
    assert public.task_id != member.task_id
    repeated, created = ensure_report_task(session, config, window)
    assert not created and repeated.task_id == public.task_id


@pytest.mark.parametrize("mismatch", [{"currency": "GBP"}, {"pricing_context": "member_discount"}])
def test_report_worker_rejects_config_drift_instead_of_exporting_another_context(session, mismatch):
    config = replace(Settings(), enabled_task_types=(TaskType.REPORT_GENERATION,))
    task, _ = ensure_report_task(session, config, utc_window(database_now(session), 3600))
    lease = claim_task(session, task.task_id, task.task_type, "context-drift", config)
    lease = replace(lease, payload={**lease.payload, **mismatch})
    with pytest.raises(TaskFailure):
        ReportGenerationHandler().prepare(lease, HandlerContext(config, None, Event()))


def test_report_worker_never_falls_back_to_an_implicit_database(session):
    config = replace(Settings(), enabled_task_types=(TaskType.REPORT_GENERATION,))
    task, _ = ensure_report_task(session, config, utc_window(database_now(session), 3600))
    lease = claim_task(session, task.task_id, task.task_type, "missing-report-factory", config)
    with pytest.raises(TaskFailure) as error:
        ReportGenerationHandler().prepare(lease, HandlerContext(config, None, Event()))
    assert error.value.failure.code == "report_session_unavailable"


def test_event_titles_and_specs_come_from_the_event_observation_not_current_fields(session):
    product, task = initial(session)
    before = database_now(session) - timedelta(minutes=2)
    product.first_seen_at, product.last_seen_at, task.scheduled_for = before, before, before
    session.flush()
    response = snapshot(captured_at=before)
    persist_product_detail(session, task, response)
    persist_snapshot(session, task, response, update_current=False)
    out = replace(response.detail, stock_status=StockStatus.OUT_OF_STOCK,
                  variants=tuple(replace(item, stock_status=StockStatus.OUT_OF_STOCK, available_quantity=0)
                                 for item in response.detail.variants))
    _, _, saved = next_observation(session, product, replace(response, detail=out), seconds=30)
    detect(session, saved["snapshot_id"])
    product.title, product.last_seen_at = "Future title", database_now(session) + timedelta(days=1)
    for variant in session.scalars(select(ProductVariant)):
        variant.options = {"Color": "Future color", "Size": "Future size"}
    session.flush()
    report = build_monitoring_report(session, Settings())
    sku_event = next(event for event in report["events"] if event["variant_id"])
    assert sku_event["title"] == "ANRABESS Cardigan"
    assert sku_event["variant_options"] == {"Color": "Apricort", "Size": "Small"}
    assert sku_event["platform_variant_id"] == "1729415858004070729"


def test_report_api_formats_and_default_write_protection(mysql_engine):
    with TestClient(create_app(settings=Settings(), engine=mysql_engine)) as client:
        json_report = client.get("/api/v1/reports/current").json()
        assert json_report["summary"]["products"] == 0
        assert client.get("/api/v1/reports/current?format=html").status_code == 200
        assert client.get("/api/v1/reports/current?format=md").status_code == 200
        assert client.get("/api/v1/reports/current?lookback_hours=0").status_code == 422
        assert client.post("/api/v1/reports/jobs", json={}).status_code == 503
        assert client.get("/api/v1/reports/jobs/missing/files/html").status_code == 404
