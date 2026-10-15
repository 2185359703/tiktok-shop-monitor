from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_analytics
from app.settings import Settings


def client_app(monkeypatch):
    builds = []
    app = FastAPI()
    app.include_router(routes_analytics.router)
    app.state.settings = Settings()

    @contextmanager
    def factory():
        yield object()

    def build(session, config, **options):
        builds.append(options)
        return {"generated_at": str(len(builds)), "query": (options.get("q") or "").strip()}

    app.state.factory = factory
    monkeypatch.setattr(routes_analytics, "build_workbench", build)
    return app, builds


def test_repeated_workbench_reads_share_only_the_same_normalized_scope(monkeypatch):
    app, builds = client_app(monkeypatch)
    with TestClient(app) as client:
        first = client.get("/api/v1/analytics/workbench?q=%20Dress%20&currency=usd").json()
        again = client.get("/api/v1/analytics/workbench?q=Dress&currency=USD").json()
        assert first == again and len(builds) == 1
        other = client.get("/api/v1/analytics/workbench?q=Shirt").json()
        assert other["query"] == "Shirt" and other["generated_at"] != first["generated_at"]
        client.get("/api/v1/analytics/workbench?q=Shirt&shop_id=2")
        client.get("/api/v1/analytics/workbench?q=Shirt&shop_id=2&lookback_hours=168")
        assert len(builds) == 4


def test_workbench_cache_does_not_leak_between_app_instances(monkeypatch):
    app, builds = client_app(monkeypatch)
    other = FastAPI()
    other.include_router(routes_analytics.router)
    other.state.settings = Settings()
    other.state.factory = app.state.factory
    with TestClient(app) as left, TestClient(other) as right:
        left.get("/api/v1/analytics/workbench")
        right.get("/api/v1/analytics/workbench")
        assert len(builds) == 2
