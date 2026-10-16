import json
import tomllib

import pytest

from deployment.release_read_models import Release


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
    instance.record.update(phase="warming", previous_images={"api": ["old-image"]})
    monkeypatch.setattr(instance, "containers", lambda service: [{"Config": {"Image": "old-image"}}])
    return instance


def test_release_preserves_all_live_compose_overrides(release):
    assert len(release.base_files) == 3
    assert release.environment["APP_IMAGE"] == "old-image"
    assert any(path.name == "compose.async.yml" for path in release.base_files)
    release.write_overlay()
    services = json.loads(release.overlay.read_text())["services"]
    assert "api" not in services
    assert services["analytics"]["environment"]["DATABASE_URL"].startswith("${")
    assert services["analytics"]["environment"]["ANALYTICS_CONCURRENCY"] == "1"
    assert services["analytics"]["command"][-1] == "projections"
    assert services["analytics-summaries"]["command"][-1] == "summaries"
    assert services["analytics-heavy"]["command"][-1] == "heavy"
    assert sum(float(services[name]["cpus"]) for name in ("analytics", "analytics-summaries", "analytics-heavy")) == 1
    assert services["api-candidate"]["ports"] == ["127.0.0.1:18081:8000"]
    release.write_overlay(public=True)
    assert json.loads(release.overlay.read_text())["services"]["analytics"]["environment"]["ANALYTICS_CONCURRENCY"] == "1"


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
