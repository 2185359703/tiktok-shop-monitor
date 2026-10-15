from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import session_dependency
from app.api.routes_reviews import router
from app.models import TaskStatus
from app.settings import Settings


class DB:
    def __init__(self, *, exists=True, count=5, status=TaskStatus.RETRY_WAITING, complete=False):
        self.exists, self.count, self.status, self.complete = exists, count, status, complete
        self.calls = 0
    def scalar(self, query):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(id=7) if self.exists else None
        if self.calls == 2:
            return self.count
        return SimpleNamespace(task_id="uuid", status=self.status, checkpoint={"complete": self.complete},
                               payload={"page_number": 2, "source_total": "38"}, attempt_count=1)
    def scalars(self, query):
        return SimpleNamespace(all=lambda: [])


def client_for(db):
    app = FastAPI()
    app.state.settings = Settings()
    app.include_router(router)
    app.dependency_overrides[session_dependency] = lambda: db
    return TestClient(app)


def test_partial_collection_is_distinct_from_finished_database_pagination(monkeypatch):
    monkeypatch.setattr("app.api.routes_reviews.reviews_available", lambda db: True)
    with client_for(DB()) as client:
        response = client.get("/api/v1/products/7/reviews")
    assert response.status_code == 200
    data = response.json()
    assert data["next_after_id"] is None and data["collection_complete"] is False
    assert data["status"] == "partial_pending" and data["collected_count"] == "5" and data["source_total"] == "38"


def test_failed_pagination_keeps_partial_status(monkeypatch):
    monkeypatch.setattr("app.api.routes_reviews.reviews_available", lambda db: True)
    with client_for(DB(status=TaskStatus.DEAD)) as client:
        assert client.get("/api/v1/products/7/reviews").json()["status"] == "partial_failed"


def test_missing_schema_and_missing_product_are_not_empty_success(monkeypatch):
    monkeypatch.setattr("app.api.routes_reviews.reviews_available", lambda db: False)
    with client_for(DB()) as client:
        data = client.get("/api/v1/products/7/reviews").json()
        assert not data["readiness"] and data["status"] == "pending_schema"
    with client_for(DB(exists=False)) as client:
        assert client.get("/api/v1/products/7/reviews").status_code == 404


def test_get_never_enqueues_and_post_requires_existing_write_auth(monkeypatch):
    def unexpected_write(*args, **kwargs):
        raise AssertionError("GET must never enqueue")
    monkeypatch.setattr("app.api.routes_reviews.reviews_available", lambda db: True)
    monkeypatch.setattr("app.api.routes_reviews.ensure_review_task", unexpected_write)
    with client_for(DB()) as client:
        assert client.get("/api/v1/products/7/reviews").status_code == 200
        assert client.post("/api/v1/products/7/reviews/collect", json={"max_pages": 3}).status_code == 503
