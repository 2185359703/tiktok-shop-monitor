"""A narrow streaming adapter for public PDP requests that need a Chrome profile."""

from contextlib import contextmanager
from urllib.request import getproxies

import httpx


class CurlResponse:
    def __init__(self, response):
        self.response = response
        self.status_code, self.headers = response.status_code, response.headers
        self.encoding = response.encoding or "utf-8"
        self.is_redirect = response.status_code in {301, 302, 303, 307, 308}

    def iter_bytes(self):
        yield from self.response.iter_content(chunk_size=65536)


class CurlHTTPClient:
    def __init__(self, proxy=None):
        from curl_cffi import requests
        from curl_cffi.requests.exceptions import RequestException
        self.request_error = RequestException
        # Respect the machine's configured HTTPS route, as HTTPX does.
        proxy = proxy or getproxies().get("https") or getproxies().get("http")
        if proxy and "://" not in proxy: proxy = "http://" + proxy
        # Keep the profile's complete default headers coherent with its TLS/H2
        # identity; overriding them differs from the successful live baseline.
        self.session = requests.Session(impersonate="chrome", proxy=proxy)

    @contextmanager
    def stream(self, method, url, **kwargs):
        try:
            with self.session.stream(method, url, timeout=20, allow_redirects=False, **kwargs) as response:
                yield CurlResponse(response)
        except self.request_error:
            raise httpx.TransportError("公开 HTTP 连接失败或超时。") from None

    def close(self):
        self.session.close()
