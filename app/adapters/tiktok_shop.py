from threading import Event

from app.crawlers.public_shop import PublicShopClient
from app.crawlers.product_list import ProductPageClient
from app.crawlers.product_detail import ProductDetailClient


class TikTokShopAdapter:
    def __init__(self, gate, settings):
        self.gate, self.settings = gate, settings

    def discover_shops(self, source_url: str, stop: Event):
        # Queue-level retry is the sole retry budget in distributed execution.
        with PublicShopClient(interval=0, max_attempts=1,
                              before_request=lambda: self.gate.wait_turn(stop)) as client:
            return client.fetch(source_url)

    def discover_products(self, source_url, pagination, stop: Event):
        # Each page owns a fresh anonymous HTTP session; no browser state copied.
        with ProductPageClient(interval=0, max_attempts=1,
                               before_request=lambda: self.gate.wait_turn(stop)) as client:
            return client.fetch_next(pagination) if pagination is not None else client.fetch_store(source_url)

    def fetch_product_detail(self, source_url, expected_shop_id, stop: Event):
        with ProductDetailClient(interval=0, max_attempts=1,
                                 http_backend=self.settings.detail_http_backend,
                                 http_proxy=self.settings.detail_proxy_url,
                                 before_request=lambda: self.gate.wait_turn(stop)) as client:
            return client.fetch_detail(source_url, expected_shop_id)
