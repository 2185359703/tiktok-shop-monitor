from contextlib import contextmanager

import httpx
import pytest

from app.crawlers.public_shop import MAX_RESPONSE_BYTES, PublicPageError, PublicShopClient
from app.settings import Settings


URL = "https://shop.tiktok.com/us/pdp/1731779511762719100"


def fake_session(monkeypatch, responses):
    from curl_cffi import requests
    class Response:
        encoding = "utf-8"
        def __init__(self, status, headers, chunks):
            self.status_code, self.headers, self.chunks = status, headers, chunks
        def iter_content(self, chunk_size):
            yield from self.chunks
    class Session:
        closed = False
        def __init__(self, **kwargs): self.arguments = kwargs
        @contextmanager
        def stream(self, method, url, **kwargs):
            yield Response(*responses.pop(0))
        def close(self): self.closed = True
    monkeypatch.setattr(requests, "Session", Session)
    return Session


def test_curl_redirects_retain_the_shared_request_gate(monkeypatch):
    fake_session(monkeypatch, [(301, {"location": URL.replace("/pdp/", "/pdp/hoodie/")}, []),
                              (200, {}, [b"public", b" body"])])
    turns = []
    with PublicShopClient(interval=0, max_attempts=1, http_backend="curl_cffi", before_request=lambda: turns.append(1)) as client:
        final, body, _ = client._read(URL)
        session = client.client.session
    assert body == b"public body" and final.endswith("/hoodie/1731779511762719100") and len(turns) == 2
    assert session.closed and session.arguments["impersonate"] == "chrome"


def test_detail_proxy_is_explicit_and_does_not_appear_in_settings_repr(monkeypatch):
    fake_session(monkeypatch, [(200, {}, [b"public"])])
    proxy = "http://user:private_password@127.0.0.1:17880"
    config = Settings(detail_proxy_url=proxy)
    assert "private_password" not in repr(config)
    with PublicShopClient(interval=0, http_backend="curl_cffi", http_proxy=proxy) as client:
        client._read(URL)
        assert client.client.session.arguments["proxy"] == proxy


def test_curl_redirects_cannot_escape_the_public_source_scope(monkeypatch):
    fake_session(monkeypatch, [(302, {"location": "https://example.com/private"}, [])])
    with PublicShopClient(interval=0, http_backend="curl_cffi") as client:
        with pytest.raises(PublicPageError): client._read(URL)


def test_curl_stream_keeps_the_existing_response_size_bound(monkeypatch):
    fake_session(monkeypatch, [(200, {}, [b"x" * (MAX_RESPONSE_BYTES + 1)])])
    with PublicShopClient(interval=0, http_backend="curl_cffi") as client:
        with pytest.raises(PublicPageError) as error: client._read(URL)
    assert error.value.code == "response_too_large"


def test_curl_errors_are_sanitized_and_reach_the_existing_retry_classification(monkeypatch):
    from curl_cffi import requests
    from curl_cffi.requests.exceptions import RequestException
    class Session:
        def __init__(self, **kwargs): pass
        @contextmanager
        def stream(self, *args, **kwargs):
            raise RequestException("private_proxy_password")
            yield
        def close(self): pass
    monkeypatch.setattr(requests, "Session", Session)
    with PublicShopClient(interval=0, max_attempts=1, http_backend="curl_cffi") as client:
        with pytest.raises(httpx.RequestError) as error: client._read(URL)
        assert "private_proxy_password" not in str(error.value)
        with pytest.raises(PublicPageError) as error: client.fetch(URL)
        assert error.value.code == "network_error" and error.value.retryable
