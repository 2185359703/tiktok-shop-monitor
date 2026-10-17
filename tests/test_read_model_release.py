import json
import tomllib

import pytest

from deployment.release_read_models import Release, upgrade_role_ready


def test_upgrade_accepts_completed_batches_but_not_failed_or_missing_runtime_roles():
    running = {"State": {"Running": True, "Status": "running", "ExitCode": 0}}
    completed = {"State": {"Running": False, "Status": "exited", "ExitCode": 0}}
    failed = {"State": {"Running": False, "Status": "exited", "ExitCode": 1}}
    oom = {"State": {"Running": False, "Status": "exited", "ExitCode": 0, "OOMKilled": True}}
    assert upgrade_role_ready("collection", [completed])
    assert upgrade_role_ready("worker", [running, running])
    for name in ("collection", "worker", "scheduler", "api"):
        assert not upgrade_role_ready(name, [])
        assert not upgrade_role_ready(name, [failed])
        assert not upgrade_role_ready(name, [oom])
    assert not upgrade_role_ready("worker", [running, completed])
    assert not upgrade_role_ready("scheduler", [completed])


@pytest.fixture
def release(tmp_path, monkeypatch):
    root = tmp_path / "tiktok-shop-monitor"
    source = root / "releases/frontend-fixture"
    (source / "app").mkdir(parents=True)
    (source / "app/main.py").write_text("")
    (root / "deployment.env").write_text("")
    (root / "deployment").mkdir()
    files = [root / "deployment" / name for name in ("compose.server.yml", "compose.frontend.yml", "compose.async.yml")]
    for path in files:
        path.write_text("services: {}\n")
    def run(self, arguments, **kwargs):
        if arguments[:2] == ["docker", "ps"]:
            return "fixture-api"
        if arguments[:2] == ["docker", "inspect"]:
            if "{{.Config.Image}}" in arguments:
                return "old-image"
            return json.dumps({"com.docker.compose.project.config_files": ",".join(str(path) for path in files)})
        raise AssertionError("Unexpected external operation")
    monkeypatch.setattr(Release, "run", run)
    instance = Release(root, "frontend-fixture", "a" * 40)
    instance.record.update(phase="warming", previous_images={name: ["old-image"] for name in ("api", "worker", "scheduler", "collection")})
    monkeypatch.setattr(instance, "containers", lambda service: [
        {"Id": "fixture-" + service, "Config": {"Image": "old-image"}, "State": {"Running": True}}
        for _ in range(2 if service == "worker" else 1)])
    return instance


def test_release_preserves_all_live_compose_overrides(release):
    assert len(release.base_files) == 3
    assert release.environment["APP_IMAGE"] == "old-image"
    assert any(path.name == "compose.async.yml" for path in release.base_files)
    release.write_overlay()
    services = json.loads(release.overlay.read_text())["services"]
    assert "api" not in services
    assert services["analytics"]["environment"]["DATABASE_URL"].startswith("${")
    assert services["analytics"]["environment"]["REDIS_URL"] == "${REDIS_URL:-redis://redis:6379/0}"
    assert services["analytics"]["environment"]["ANALYTICS_CONCURRENCY"] == "1"
    assert services["analytics"]["command"][-1] == "projections"
    assert services["analytics-summaries"]["command"][-1] == "summaries"
    assert services["analytics-heavy"]["command"][-1] == "heavy"
    assert sum(float(services[name]["cpus"]) for name in ("analytics", "analytics-summaries", "analytics-heavy")) == 1
    assert services["api-candidate"]["ports"] == ["127.0.0.1:18081:8000"]
    release.write_overlay(public=True)
    assert json.loads(release.overlay.read_text())["services"]["analytics"]["environment"]["ANALYTICS_CONCURRENCY"] == "1"


def test_interrupted_upgrade_preserves_original_rollback_snapshot(release, monkeypatch):
    release.record["phase"] = "upgrade_prepared"
    saved = release.evidence / "previous-live-overlay.json"
    saved.write_text('{"original": true}\n')
    release.overlay.write_text('{"staging": true}\n')
    monkeypatch.setattr(release, "read", lambda *args, **kwargs: pytest.fail("Must preserve the interrupted upgrade before reading or writing"))
    with pytest.raises(ValueError, match="preserved rollback snapshot"):
        release.upgrade()
    assert saved.read_text() == '{"original": true}\n'
    assert release.overlay.read_text() == '{"staging": true}\n'


def test_upgrade_repairs_v1_collection_columns_before_strict_schema_check(release, monkeypatch, mysql_engine):
    import importlib
    import io
    from contextlib import redirect_stdout

    from app.storage.schema import schema_drift
    from scripts import migrate_analytics_rollups, migrate_collection_plan_archive

    # Model the deployed 012 table: later additive tables may already exist,
    # but the existing plan table still needs 015 before any full-schema check.
    with mysql_engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE collection_plans DROP COLUMN archived_at")
    monkeypatch.setattr(migrate_analytics_rollups, "make_engine", lambda: mysql_engine)
    try:
        with pytest.raises(ValueError, match="existing schema has drift"):
            migrate_analytics_rollups.main(["--check"])

        release.record.clear()
        original_overlay = '{"services": {"api": {"image": "old-image"}}}\n'
        release.overlay.write_text(original_overlay)
        previous = release.containers
        monkeypatch.setattr(release, "containers", lambda name: [
            {**item, "Image": "old-image-id", "NetworkSettings": {"Networks": {"fixture-network": {}}}}
            for item in previous(name)])
        monkeypatch.setattr(release, "read", lambda *args, **kwargs: {"ready": True})
        monkeypatch.setattr(release, "runtime_config", lambda api: {})
        migrated, started = [], []

        def run(arguments, **kwargs):
            if arguments[:3] == ["docker", "image", "inspect"]:
                return release.commit
            assert arguments[:3] == ["docker", "run", "--rm"]
            name = arguments[arguments.index("-m") + 1]
            module = importlib.import_module(name)
            monkeypatch.setattr(module, "make_engine", lambda: mysql_engine)
            output = io.StringIO()
            with redirect_stdout(output):
                assert module.main(["--execute"]) == 0
            migrated.append(name)
            return output.getvalue().strip()

        def up(*services, **kwargs):
            assert schema_drift(mysql_engine) == []
            assert "scripts.migrate_analytics_rollups" in migrated
            started.extend(services)

        monkeypatch.setattr(release, "run", run)
        monkeypatch.setattr(release, "up", up)
        release.upgrade()
        assert release.record["phase"] == "warming"
        assert started == ["analytics", "analytics-summaries", "analytics-heavy", "api-candidate"]
        assert (release.evidence / "previous-live-overlay.json").read_text() == original_overlay
        assert migrate_analytics_rollups.main(["--check"]) == 0
    finally:
        # The session-scoped disposable schema must be current for later tests,
        # including when the original ordering reproduces the deployment error.
        migrate_collection_plan_archive.migrate(mysql_engine, execute=True)


def test_release_cannot_switch_before_backfill_finishes(release, monkeypatch):
    monkeypatch.setattr(release, "read", lambda *args, **kwargs: {"ready": False})
    with pytest.raises(ValueError, match="backfill"):
        release.cutover()
    assert not release.overlay.exists()


def test_release_cannot_overwrite_another_public_deployment(release, monkeypatch):
    monkeypatch.setattr(release, "containers", lambda service: [{"Config": {"Image": "newer-unrelated-release"}}])
    with pytest.raises(ValueError, match="another release"):
        release.cutover()
    assert not release.overlay.exists()


def test_release_cannot_overwrite_changed_workers_while_api_is_unchanged(release, monkeypatch):
    previous = release.containers
    monkeypatch.setattr(release, "containers", lambda name: [
        {**item, "Config": {"Image": "new-unreviewed-worker"}} for item in previous(name)] if name == "worker" else previous(name))
    with pytest.raises(ValueError, match="another release: worker"):
        release.cutover()
    assert not release.overlay.exists()


def test_recorded_worker_only_patch_can_be_upgraded_with_the_api(release, monkeypatch):
    previous = release.containers
    release.record["previous_images"]["worker"] = ["recorded-review-worker"]
    monkeypatch.setattr(release, "containers", lambda name: [
        {**item, "Config": {"Image": "recorded-review-worker"}} for item in previous(name)] if name == "worker" else previous(name))
    monkeypatch.setattr(release, "read", lambda *args, **kwargs: {"ready": False})
    with pytest.raises(ValueError, match="backfill"):
        release.cutover()
    assert not release.overlay.exists()


def test_release_keeps_active_request_interval_and_worker_concurrency(release, monkeypatch):
    def run(arguments, **kwargs):
        if arguments[-1].endswith("server.toml"):
            return '[scope]\nplatform="tiktok_shop"\n[runtime]\nenabled_task_types=["product_detail"]\n[request]\nminimum_interval_seconds=2.0\n'
        return json.dumps({"worker_concurrency": 4, "enabled_task_types": ["product_detail"], "minimum_interval_seconds": 1.0})
    monkeypatch.setattr(release, "run", run)
    result = release.runtime_config({"Id": "fixture", "Config": {"Env": ["SETTINGS_FILE=/opt/configs/server.toml"]}})
    saved = tomllib.loads((release.source / "runtime-server.toml").read_text())
    assert saved["request"]["minimum_interval_seconds"] == result["minimum_interval_seconds"] == 1.0
    assert saved["runtime"]["worker_concurrency"] == 4


@pytest.fixture
def collection_candidate_responses():
    return {
        "/api/v1/collection-management/control": {"platform": "tiktok_shop", "region": "US",
            "paused": False, "writes_enabled": True, "supported_task_types": ["shop_profile"],
            "enabled_task_types": ["shop_profile"], "limits": {"max_resources": 100}},
        "/api/v1/collection-management/plans?limit=1": {"items": [], "next_before_id": None},
        "/api/v1/collection-management/audit?limit=1": {"items": [], "next_before_id": None},
        "/api/v1/collection-management/launches?limit=1": {"items": [], "next_before_id": None},
        "/api/v1/collection-management/resources?task_type=shop_profile&limit=1": {
            "items": [{"id": "1", "eligible": True}], "next_before_id": None},
        "/api/v1/collections/monitor?window_minutes=60": {
            "generated_at": "2026-10-17T00:00:00+00:00", "scope": {"platform": "tiktok_shop", "region": "US"},
            "window": {"minutes": 60}, "queue": {"status": "pending"}, "attempts": {"total": None},
            "task_group": "source", "statistics": {"method": "minute_rollup", "ready": False, "status": "warming"},
            "leases": {"items": []}, "runtime": {"status": "available", "components": []},
            "trend": [], "errors": [], "recent_failures": [], "alerts": []},
        "/api/v1/collections/alerts?status=active&limit=1": {"items": [], "next_before_id": None, "evaluation": {"status": "never"}},
    }


def test_candidate_collection_reads_allow_pending_statistics_and_no_new_heartbeats(release, monkeypatch, collection_candidate_responses):
    calls = []
    def read(path, **kwargs):
        calls.append((path, kwargs))
        return collection_candidate_responses[path]
    monkeypatch.setattr(release, "read", read)
    release.check_collection_candidate()
    assert [path for path, _ in calls] == list(collection_candidate_responses)
    assert all(options["port"] == 18081 for _, options in calls)
    assert not release.overlay.exists()


def test_collection_monitor_activation_requires_all_writers_on_the_release(release, monkeypatch):
    def containers(name):
        return [{"Id": f"fixture-{name}-{i}", "Config": {"Image": release.image},
                 "State": {"Running": name != "collection", "Status": "exited" if name == "collection" else "running", "ExitCode": 0}}
                for i in range(2 if name == "worker" else 1)]
    calls = []
    monkeypatch.setattr(release, "containers", containers)
    monkeypatch.setattr(release, "run", lambda argv, **kwargs: calls.append(argv) or '{"activated": true}')
    release.reconcile_collection_monitor()
    assert len(calls) == 1 and calls[0][-1] == "--reconcile"
    good = containers
    monkeypatch.setattr(release, "containers", lambda name: [
        {**item, "Config": {"Image": "old-image"}} for item in good(name)] if name == "worker" else good(name))
    with pytest.raises(ValueError, match="Every collection writer"):
        release.reconcile_collection_monitor()
    assert len(calls) == 1


def test_failed_switch_restores_all_previous_application_roles(release, monkeypatch, collection_candidate_responses):
    names = ("api", "worker", "scheduler", "collection", "analytics", "analytics-summaries", "analytics-heavy")
    release.record["previous_images"] = {name: ["old-image"] for name in names}
    def read(url, **kwargs):
        return {"ready": True} if url == "/api/v2/ui/bootstrap" else collection_candidate_responses.get(url, {})
    monkeypatch.setattr(release, "read", read)
    calls = []
    def up(*services, **kwargs):
        calls.append((services, kwargs))
        if len(calls) == 1:
            raise RuntimeError("fixture rollout failure")
    monkeypatch.setattr(release, "up", up)
    with pytest.raises(RuntimeError, match="fixture rollout failure"):
        release.cutover()
    assert calls[1][0] == names
    assert release.record["phase"] == "cutover_failed_old_application_restored"


@pytest.mark.parametrize("path,invalid", [
    ("/api/v1/collection-management/control", {"paused": "false"}),
    ("/api/v1/collection-management/plans?limit=1", {"items": [], "next_before_id": 123}),
    ("/api/v1/collection-management/audit?limit=1", {"items": "not-a-page", "next_before_id": None}),
    ("/api/v1/collections/monitor?window_minutes=60", {"error": "database_unavailable"}),
])
def test_malformed_collection_candidate_prevents_cutover(release, monkeypatch, collection_candidate_responses, path, invalid):
    collection_candidate_responses[path] = invalid
    def read(url, **kwargs):
        if url == "/api/v2/ui/bootstrap":
            return {"ready": True}
        return collection_candidate_responses.get(url, {})
    monkeypatch.setattr(release, "read", read)
    monkeypatch.setattr(release, "up", lambda *args, **kwargs: pytest.fail("Candidate validation must precede service changes"))
    with pytest.raises(ValueError, match="Candidate collection"):
        release.cutover()
    assert not release.overlay.exists()


def test_candidate_monitor_cannot_silently_switch_market(release, monkeypatch, collection_candidate_responses):
    collection_candidate_responses["/api/v1/collections/monitor?window_minutes=60"]["scope"]["region"] = "GB"
    monkeypatch.setattr(release, "read", lambda path, **kwargs: collection_candidate_responses[path])
    with pytest.raises(ValueError, match="Candidate collection monitor"):
        release.check_collection_candidate()
