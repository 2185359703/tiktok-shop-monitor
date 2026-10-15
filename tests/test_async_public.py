import asyncio
import json

import httpx
import pytest

from app.crawlers.async_public import AsyncPublicClient
from app.crawlers.public_shop import PublicPageError
from test_product_detail import html as detail_html, SID, URL
from test_product_reviews import URL as REVIEW_URL


def test_detail_request_and_parser_remain_native_async_and_preserve_identity():
    async def run():
        active = peak = 0
        async def route(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.03)
            active -= 1
            return httpx.Response(200, text=detail_html())
        async def fetch():
            async with AsyncPublicClient(backend='httpx', transport=httpx.MockTransport(route)) as client:
                return await client.fetch(URL, kind='detail', expected_shop_id=SID)
        first, second = await asyncio.gather(fetch(), fetch())
        assert peak == 2 and first.detail.platform_shop_id == SID
        assert first.detail == second.detail and first.response_hash == second.response_hash
    asyncio.run(run())


def test_redirect_outside_public_scope_is_rejected_before_following():
    async def run():
        requests = []
        def route(request):
            requests.append(request)
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
        async with AsyncPublicClient(backend='httpx', transport=httpx.MockTransport(route)) as client:
            with pytest.raises(PublicPageError):
                await client.fetch(REVIEW_URL, kind='detail', expected_shop_id=SID)
        assert len(requests) == 1
    asyncio.run(run())


def test_detail_proxy_does_not_change_store_or_listing_transport(monkeypatch):
    from app.crawlers.async_public import AsyncTikTokShopAdapter
    from app.settings import Settings
    seen = []
    class Client:
        def __init__(self, **kwargs): seen.append(kwargs)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def fetch(self, *args, **kwargs): return kwargs['kind']
    class Gate:
        async def wait_turn(self): pass
    monkeypatch.setattr('app.crawlers.async_public.AsyncPublicClient', Client)
    async def run():
        adapter = AsyncTikTokShopAdapter(Gate(), Settings(detail_http_backend='curl_cffi',
                                                         detail_proxy_url='http://proxy.invalid:8080'))
        for kind in ('shops', 'profile', 'products', 'detail'):
            assert await adapter.request(URL, kind) == kind
        assert all(row['proxy'] is None and row['backend'] == 'httpx' for row in seen[:3])
        assert seen[3]['proxy'] == 'http://proxy.invalid:8080' and seen[3]['backend'] == 'curl_cffi'
    asyncio.run(run())
