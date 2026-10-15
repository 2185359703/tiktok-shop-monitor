from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import session_dependency
from app.api.routes_content import router
from app.crawlers.product_content import parse_product_content
from app.models import ProductContent
from app.settings import Settings


class ReadSession:
    def __init__(self, content=None, exists=True):
        self.content, self.exists, self.calls = content, exists, 0

    def scalar(self, query):
        self.calls += 1
        return SimpleNamespace(id=7) if self.calls == 1 and self.exists else self.content


def client_for(db):
    app = FastAPI()
    app.state.settings = Settings()
    app.include_router(router)
    app.dependency_overrides[session_dependency] = lambda: db
    return TestClient(app)


def test_unmigrated_api_distinguishes_schema_readiness_from_product_coverage(monkeypatch):
    monkeypatch.setattr("app.api.routes_content.product_content_table_available", lambda db: False)
    with client_for(ReadSession()) as client:
        value = client.get("/api/v1/products/7/content").json()
    assert not value["readiness"] and value["status"] == "pending_schema" and value["content"] is None
    monkeypatch.setattr("app.api.routes_content.product_content_table_available", lambda db: True)
    with client_for(ReadSession()) as client:
        value = client.get("/api/v1/products/7/content").json()
    assert value["readiness"] and value["status"] == "not_captured" and not value["available"]


def test_merchant_content_api_preserves_order_parameters_and_independent_provenance(monkeypatch):
    monkeypatch.setattr("app.api.routes_content.product_content_table_available", lambda db: True)
    parsed = parse_product_content({"specifications": [{"name": "Lining", "value": "Polyester"}],
                                   "desc_blocks": [{"type": "text", "text": "Merchant coat"}]})
    row = ProductContent(product_id=7, description_text=parsed.description_text, sections=parsed.sections,
                         completeness=parsed.completeness, unsupported_blocks=[], source_shape=parsed.source_shape,
                         source_version=parsed.source_version, content_hash="a" * 64, content_version=1,
                         source_url="https://shop.tiktok.com/us/pdp/12345", response_hash="b" * 64,
                         observed_at=datetime.now(timezone.utc))
    with client_for(ReadSession(row)) as client:
        response = client.get("/api/v1/products/7/content")
    value = response.json()
    assert response.status_code == 200 and value["available"] and value["readiness"]
    assert value["content"]["sections"]["specifications"][0]["value"] == "Polyester"
    assert value["content"]["description_text"] == "Merchant coat"
    assert value["content"]["completeness"] == "complete" and value["content"]["source_version"] == parsed.source_version
    assert "price_min" not in value["content"] and "response_hash" not in value["content"]


def test_content_unknown_product_is_404():
    with client_for(ReadSession(exists=False)) as client:
        assert client.get("/api/v1/products/7/content").status_code == 404
