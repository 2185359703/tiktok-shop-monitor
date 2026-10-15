"""Async collection clients reuse the established identity and data parsers."""

import asyncio
import time
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import urljoin

from app.crawlers.async_http import AsyncHTTPClient
from app.crawlers.public_shop import PublicPageError, PublicPageSnapshot, parse_shop_candidates, public_url, shop_id_from_url
from app.crawlers.product_list import (PRODUCT_LIST_ENDPOINT, ProductPageSnapshot, annotate_failure,
    parse_product_page_json, parse_store_products, product_id_from_url)
from app.crawlers.product_detail import DetailSnapshot, parse_product_detail
from app.crawlers.shop_profile import ShopProfilePageSnapshot, parse_shop_profile


class AsyncPublicClient(AsyncHTTPClient):
    async def document(self, source_url):
        url = public_url(source_url)
        for hop in range(4):
            status, location, body = await self.read("GET", url)
            if status == 200:
                return url, body
            if not location or hop == 3:
                raise PublicPageError("redirect_error", "公开入口重定向异常。")
            url = public_url(urljoin(url, location))
        raise AssertionError("unreachable")

    async def fetch(self, source_url, *, kind, expected_shop_id=None, pagination=None):
        began, start, body = datetime.now(timezone.utc), time.monotonic(), None
        source_url = PRODUCT_LIST_ENDPOINT if pagination is not None else public_url(source_url)
        try:
            if pagination is not None:
                _, _, body = await self.read("POST", PRODUCT_LIST_ENDPOINT, json=pagination.request_payload(),
                                              headers={"Accept": "application/json"})
                final = PRODUCT_LIST_ENDPOINT
                parsed = await asyncio.to_thread(parse_product_page_json, body, pagination)
            else:
                final, body = await self.document(source_url)
                html = body.decode("utf-8", errors="replace")
                if kind == "detail":
                    if product_id_from_url(final) != product_id_from_url(source_url):
                        raise PublicPageError("product_detail_redirect_conflict", "商品重定向身份不一致。", status=200)
                    parsed = await asyncio.to_thread(parse_product_detail, html, final, expected_shop_id)
                elif kind == "profile":
                    if not shop_id_from_url(source_url) or shop_id_from_url(final) != shop_id_from_url(source_url):
                        raise PublicPageError("store_identity_mismatch", "店铺重定向身份不一致。", status=200)
                    parsed = await asyncio.to_thread(parse_shop_profile, html, final)
                elif kind == "products":
                    parsed = await asyncio.to_thread(parse_store_products, html, final)
                else:
                    parsed = await asyncio.to_thread(parse_shop_candidates, html, final)
        except PublicPageError as error:
            if kind == "detail" and error.code in {"invalid_product_detail_structure", "missing_product_detail_component", "invalid_product_detail_router"}:
                error.retryable = True
            annotate_failure(error, source_url, began, body)
            raise
        elapsed = round((time.monotonic() - start) * 1000)
        common = (source_url, final, 200, len(body), sha256(body).hexdigest(), elapsed)
        captured = datetime.now(timezone.utc)
        if kind == "detail":
            return DetailSnapshot(*common, began, captured, parsed)
        if kind == "products":
            return ProductPageSnapshot(*common, began, captured, parsed)
        if kind == "profile":
            return ShopProfilePageSnapshot(*common, 1, began, captured, parsed)
        return PublicPageSnapshot(*common, 1, began, captured, parsed)


class AsyncTikTokShopAdapter:
    def __init__(self, gate, settings):
        self.gate, self.settings = gate, settings

    async def request(self, source_url, kind, **kwargs):
        async with AsyncPublicClient(before_request=self.gate.wait_turn,
            backend=self.settings.detail_http_backend if kind == "detail" else "httpx",
            proxy=self.settings.detail_proxy_url) as client:
            return await client.fetch(source_url, kind=kind, **kwargs)

    async def discover_shops(self, source_url, stop=None):
        return await self.request(source_url, "shops")

    async def fetch_shop_profile(self, source_url, stop=None):
        return await self.request(source_url, "profile")

    async def discover_products(self, source_url, pagination, stop=None):
        return await self.request(source_url, "products", pagination=pagination)

    async def fetch_product_detail(self, source_url, expected_shop_id, stop=None):
        return await self.request(source_url, "detail", expected_shop_id=expected_shop_id)
