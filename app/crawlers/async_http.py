"""Native async, bounded streaming HTTP shared by every public collector."""

import asyncio
from urllib.request import getproxies

import httpx

from app.crawlers.public_shop import MAX_RESPONSE_BYTES, PublicPageError


class AsyncHTTPClient:
    def __init__(self, *, before_request=None, proxy=None, backend="curl_cffi", transport=None):
        self.before_request, self.proxy, self.backend, self.transport = before_request, proxy, backend, transport

    async def __aenter__(self):
        proxy = self.proxy or getproxies().get("https") or getproxies().get("http")
        if proxy and "://" not in proxy:
            proxy = "http://" + proxy
        if self.backend == "curl_cffi":
            from curl_cffi.requests import AsyncSession
            self.client = AsyncSession(impersonate="chrome", proxy=proxy, max_clients=1)
        elif self.backend == "httpx":
            self.client = httpx.AsyncClient(proxy=None if self.transport else proxy, transport=self.transport,
                follow_redirects=False, timeout=httpx.Timeout(20, connect=10),
                headers={"User-Agent": "tiktok-shop-monitor/0.1 (public-source-discovery)",
                         "Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"})
        else:
            raise ValueError("Unsupported public HTTP backend")
        return self

    async def __aexit__(self, *args):
        if self.backend == "curl_cffi":
            await self.client.close()
        else:
            await self.client.aclose()

    async def read(self, method, url, **kwargs):
        if self.before_request:
            await self.before_request()
        try:
            async with asyncio.timeout(30):
                stream = self.client.stream(method, url, **kwargs, **(
                    {"allow_redirects": False, "timeout": 20} if self.backend == "curl_cffi" else {}))
                async with stream as response:
                    if response.status_code in {301, 302, 303, 307, 308} and method == "GET":
                        return response.status_code, response.headers.get("location"), b""
                    if response.status_code in {401, 403, 429}:
                        raise PublicPageError("access_limited", "公开来源要求认证或限流。", status=response.status_code, retryable=True)
                    if response.status_code != 200:
                        raise PublicPageError("http_error", "公开请求返回非成功状态。", status=response.status_code,
                                              retryable=response.status_code >= 500)
                    chunks, size = [], 0
                    iterator = response.aiter_content() if self.backend == "curl_cffi" else response.aiter_bytes()
                    async for chunk in iterator:
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise PublicPageError("response_too_large", "公开响应超过8 MiB。", status=200)
                        chunks.append(chunk)
                    return 200, None, b"".join(chunks)
        except (httpx.RequestError, TimeoutError):
            raise PublicPageError("network_error", "公开请求连接失败或超时。", retryable=True) from None
        except Exception as error:
            if self.backend == "curl_cffi":
                from curl_cffi.requests.exceptions import RequestException
                if isinstance(error, RequestException):
                    raise PublicPageError("network_error", "公开请求连接失败或超时。", retryable=True) from None
            raise

