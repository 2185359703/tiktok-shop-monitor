from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import session_dependency
from app.api.routes_images import router
from app.models.product_image import ProductImage
from app.settings import Settings
from test_product_images import IMAGE, THUMB


class ReadSession:
    def __init__(self, rows=(), product_exists=True):
        self.rows = rows
        self.product_exists = product_exists

    def scalar(self, statement):
        return SimpleNamespace(id=7) if self.product_exists else None

    def scalars(self, statement):
        return SimpleNamespace(all=lambda: self.rows)


def client_for(session):
    app = FastAPI()
    app.state.settings = Settings()
    app.include_router(router)
    app.dependency_overrides[session_dependency] = lambda: session
    return TestClient(app)


def test_api_states_distinguish_missing_migration_from_missing_photos(monkeypatch):
    monkeypatch.setattr("app.api.routes_images.images_table_available", lambda db: False)
    with client_for(ReadSession()) as client:
        data = client.get("/api/v1/products/7/images").json()
    assert data["status"] == "pending_schema" and data["items"] == []
    monkeypatch.setattr("app.api.routes_images.images_table_available", lambda db: True)
    with client_for(ReadSession()) as client:
        data = client.get("/api/v1/products/7/images").json()
    assert data["status"] == "not_captured" and data["items"] == []


def test_api_returns_source_order_and_real_urls_without_fake_fallback(monkeypatch):
    monkeypatch.setattr("app.api.routes_images.images_table_available", lambda db: True)
    row = ProductImage(id=3, product_id=7, image_key="a" * 64, image_url=IMAGE, thumbnail_url=THUMB,
                       source_url="https://shop.tiktok.com/us/pdp/1729415858004005193", source_kind="detail",
                       source_version="public_product_images_v1", role="cover", position=0, is_current=True,
                       observed_at=datetime.now(timezone.utc))
    with client_for(ReadSession([row])) as client:
        response = client.get("/api/v1/products/7/images")
    data = response.json()
    assert response.status_code == 200 and data["status"] == "available"
    assert data["items"][0]["image_url"] == IMAGE and data["items"][0]["thumbnail_url"] == THUMB
    assert data["items"][0]["position"] == 0 and data["items"][0]["source_kind"] == "detail"


def test_unknown_product_is_404():
    with client_for(ReadSession(product_exists=False)) as client:
        assert client.get("/api/v1/products/7/images").status_code == 404
