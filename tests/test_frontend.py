from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.settings import Settings


FRONTEND_DIRECTORY = Path(__file__).resolve().parents[1] / "app" / "frontend"


@pytest.fixture
def static_client(monkeypatch, tmp_path):
    def unexpected_database_use(*args, **kwargs):
        raise AssertionError("Frontend requests must not load settings or use a database")

    class NoDatabase:
        connect = unexpected_database_use
        dispose = unexpected_database_use

    monkeypatch.setattr(main, "make_engine", unexpected_database_use)
    monkeypatch.setattr(main.Settings, "load", unexpected_database_use)
    monkeypatch.setattr(main, "make_session_factory", lambda engine: unexpected_database_use)
    # Production paths must resolve from the module even when launched elsewhere.
    monkeypatch.chdir(tmp_path)
    with TestClient(main.create_app(settings=Settings(), engine=NoDatabase())) as client:
        yield client


def test_root_serves_frontend_with_lifespan_and_without_database_use(static_client):
    response = static_client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.content == (FRONTEND_DIRECTORY / "index.html").read_bytes()


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [("styles.css", "text/css"), ("app.js", "javascript"), ("api.js", "javascript"),
     ("charts.js", "javascript"), ("product-table.js", "javascript"), ("comparison.js", "javascript"),
     ("workbench.js", "javascript"), ("matching-ui.js", "javascript"), ("workbench.css", "text/css")],
)
def test_static_assets_are_served_from_the_module_directory(static_client, filename, content_type):
    response = static_client.get(f"/ui-assets/{filename}")
    assert response.status_code == 200
    assert content_type in response.headers["content-type"]
    assert response.content == (FRONTEND_DIRECTORY / filename).read_bytes()
    assert response.content


def test_static_mount_does_not_expose_parent_files_or_swallow_api_routes(static_client):
    assert static_client.get("/ui-assets/%2e%2e/main.py").status_code == 404
    assert static_client.get("/ui-assets/missing.js").status_code == 404
    assert static_client.get("/api/v1/missing").status_code == 404
    assert static_client.get("/health/live").json() == {"status": "ok"}
    paths = static_client.get("/openapi.json").json()["paths"]
    assert "/api/v1/products" in paths
    assert "/api/v1/reports/current" in paths
    assert "/api/v1/collections/current" in paths
    assert "/" not in paths
