"""Exercise the shipped frontend in an isolated Chromium with memory-only APIs.

Run: python tests/frontend/product_content_browser.py --browser-cache D:/Chrome/Playwright
Requires Python Playwright and an installed Chromium. No database, TikTok request,
existing browser profile, or development server is used. Port 8026 is fixture-only.
"""

import argparse
import json
import mimetypes
import os
import struct
import zlib
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.parse import unquote, urlsplit

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "app" / "frontend"
PHOTO = "https://images.example.com/fixture-coat.png"
DATE = "2026-10-15T00:00:00Z"


def fixture_png():
    """A deterministic long image, generated locally without optional libraries."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    colors = [bytes([222, 231, 247]), bytes([235, 239, 247]), bytes([203, 218, 240])]
    pixels = b"".join(b"\x00" + colors[(row // 300) % len(colors)] * 800 for row in range(1800))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 800, 1800, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


PNG = fixture_png()


def product(identifier):
    return {"id": str(identifier), "platform_product_id": f"10000{identifier}",
            "title": f"Fixture garment {identifier}", "shop_id": "1", "shop_name": "Fixture apparel",
            "currency": "USD", "pricing_context": "public_listed", "price_min": "29.99",
            "price_max": "39.99", "stock_status": "in_stock", "rating": "4.6", "review_count": 825,
            "category": "Clothing > Coats", "original_category": "Clothing > Coats", "normalized_category": "Coats",
            "last_seen_at": DATE, "last_success_at": DATE, "description": "Legacy text <not HTML>",
            "detail_collected": True, "sku_count": 0, "thumbnail_url": PHOTO, "image_url": PHOTO}


def content(identifier, completeness="complete"):
    specifications = [{"name": "Lining description", "value": "Polyester & wool <blend>", "source": "specifications"}]
    description = [
        {"type": "heading", "text": "Material & Construction", "level": 2},
        {"type": "paragraph", "text": "<script>window.__contentXss = true</script>\nMerchant description."},
        {"type": "list", "ordered": True, "start": 4, "items": ["Soft <fabric>", "Machine washable"]},
        {"type": "table", "rows": [
            [{"text": "Size guide", "header": True, "rowspan": 1, "colspan": 2},
             *[{"text": f"Measurement {i}", "header": True, "rowspan": 1, "colspan": 1} for i in range(5)]],
            [{"text": value, "header": False, "rowspan": 1, "colspan": 1}
             for value in ["S", "4–6", "40.2", "27.8", "52.8", "44.9", '<img src=x onerror="window.__contentXss=true">']]]},
        {"type": "image", "url": PHOTO, "alt": "Merchant coat details", "role": "description", "width": 800, "height": 1800},
        {"type": "image", "url": "http://127.0.0.1/forbidden-private.png", "alt": "Blocked", "role": "description"},
        {"type": "image", "url": "javascript:window.__contentXss=true", "alt": "Blocked", "role": "description"},
    ]
    if completeness in {"empty", "missing"}:
        specifications, description = [], []
    projection = {"sections": {"specifications": specifications, "description": description},
                  "description_text": None, "completeness": completeness,
                  "unsupported_blocks": ["desc_blocks:2:unsupported_video"] if completeness == "partial" else [],
                  "source_version": "public_product_content_v1", "observed_at": DATE}
    if completeness == "partial":
        projection["observed_at"] = "2026-10-13T04:00:00Z"
        projection["section_observations"] = {
            "specifications": {"observed_at": "2026-10-13T04:00:00Z", "completeness": "complete"},
            "description": {"observed_at": DATE, "completeness": "partial"}}
        projection["latest_observation"] = {"observed_at": "2026-10-15T01:00:00Z", "completeness": "partial"}
    return {"product_id": str(identifier), "readiness": True, "available": True, "content": projection}


class Fixture:
    def __init__(self):
        self.counts, self.lock = Counter(), Lock()
        self.release = {"8": Event(), "9": Event()}
        self.entered = {"8": Event(), "9": Event()}

    def count(self, path):
        with self.lock:
            return self.counts[path]

    def handler(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, data, status=200, content_type="application/json"):
                body = json.dumps(data, ensure_ascii=False).encode() if content_type == "application/json" else data
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # Browser cancellation is one of the tested outcomes.

            def do_GET(self):
                path = urlsplit(self.path).path
                with fixture.lock:
                    fixture.counts[path] += 1
                    attempt = fixture.counts[path]
                if path == "/":
                    return self.send((FRONTEND / "index.html").read_bytes(), content_type="text/html")
                if path.startswith("/ui-assets/"):
                    target = (FRONTEND / unquote(path.removeprefix("/ui-assets/"))).resolve()
                    if not target.is_relative_to(FRONTEND) or not target.is_file():
                        return self.send({}, 404)
                    kind = "text/javascript" if target.suffix == ".js" else mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    return self.send(target.read_bytes(), content_type=kind)
                if path == "/api/v1/overview":
                    return self.send({"shops_with_url": 1, "products": 10})
                if path == "/api/v1/capabilities":
                    return self.send({"region": "US", "platform": "tiktok_shop", "writes_enabled": False})
                if path == "/api/v1/shops":
                    return self.send({"items": [{"id": "1", "name": "Fixture apparel"}], "next_after_id": None})
                if path == "/api/v1/analytics/products":
                    return self.send({"items": [product(i) for i in range(1, 11)], "total": 10,
                                      "page": 1, "page_size": 20, "total_pages": 1, "scope": {"currency": "USD"}})
                if path == "/api/v1/analytics/workbench":
                    return self.send({"scope": {"currency": "USD"}, "generated_at": DATE,
                                      "summary": {"products": 10, "shops": 1, "price_known_products": 0},
                                      "history_summary": {}, "shops": [], "categories": []})
                parts = path.split("/")
                if len(parts) >= 5 and parts[3] == "products":
                    identifier = parts[4]
                    if len(parts) == 5:
                        return self.send(product(identifier))
                    resource = parts[5]
                    if resource in {"variants", "snapshots"}:
                        return self.send({"items": [], "next_after_id": None})
                    if resource == "images":
                        return self.send({"items": [{"image_url": PHOTO, "role": "cover"}]})
                    if resource == "content":
                        if identifier in fixture.release:
                            fixture.entered[identifier].set()
                            fixture.release[identifier].wait(10)
                        if identifier == "7" and attempt == 1:
                            return self.send({"detail": "temporary_fixture_error"}, 503)
                        if identifier in {"5", "6"}:
                            return self.send({"product_id": identifier, "readiness": identifier != "6",
                                              "available": False, "content": None})
                        data = content(identifier, {"2": "partial", "3": "empty", "4": "missing"}.get(identifier, "complete"))
                        if identifier == "10":
                            data["product_id"] = "wrong-product"
                        return self.send(data)
                return self.send({"detail": "fixture_route_not_found"}, 404)

        return Handler


def run(args):
    if args.browser_cache:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = args.browser_cache
    fixture = Fixture()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), fixture.handler())
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    checks, errors, blocked, failed = [], [], [], []
    origin = f"http://127.0.0.1:{args.port}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")

            def route_request(route):
                if route.request.url.startswith(origin + "/"):
                    route.continue_()
                elif route.request.url == PHOTO:
                    route.fulfill(status=200, content_type="image/png", body=PNG)
                else:
                    blocked.append(route.request.url)
                    route.abort()

            context.route("**/*", route_request)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("requestfailed", lambda request: failed.append({"url": request.url, "failure": request.failure}))
            page.goto(origin + "/#products")
            expect(page.locator('.product-title[data-id="1"]')).to_be_visible()
            expect(page.locator("#connection-label")).to_have_text("数据已连接")

            def open_product(identifier, source=None):
                (source or page).locator(f'.product-title[data-action="product"][data-id="{identifier}"]').first.click()
                expect(page.locator("#pi-tab-overview")).to_be_visible()
                expect(page.locator("#dialog-title")).to_have_text(f"Fixture garment {identifier}")

            def close_product():
                page.locator('.catalog-dialog-top [data-action="close-dialog"]').click()
                expect(page.locator("#detail-dialog")).not_to_be_visible()

            open_product("1")
            assert fixture.count("/api/v1/products/1/content") == 0
            # A future content count slot must never turn this object into an items list.
            page.locator("#pi-tab-info small").evaluate("el => el.dataset.piCount = 'content'")
            page.locator("#pi-tab-info").click()
            expect(page.locator(".pi-content-specifications")).to_contain_text("Polyester & wool <blend>")
            expect(page.locator(".pi-description-body")).to_contain_text("<script>window.__contentXss = true</script>")
            expect(page.locator(".pi-description-list")).to_have_attribute("start", "4")
            expect(page.locator('.pi-content-table th[colspan="2"]')).to_have_text("Size guide")
            expect(page.locator(".pi-description-figure")).to_have_count(1)
            image = page.locator(".pi-description-figure img")
            expect(image).to_have_attribute("loading", "lazy")
            image.scroll_into_view_if_needed()
            expect(image).to_have_js_property("naturalWidth", 800)
            expect(image).to_have_js_property("naturalHeight", 1800)
            assert image.evaluate("el => el.clientHeight > el.clientWidth && el.clientWidth <= el.closest('#pi-panel').clientWidth")
            assert page.evaluate("window.__contentXss") is None
            assert page.locator("#pi-panel script,#pi-panel iframe,#pi-panel img[onerror]").count() == 0
            assert page.locator(".pi-media .pi-description-figure").count() == 0
            page.locator("#pi-tab-overview").click()
            page.locator("#pi-tab-info").click()
            expect(page.locator(".pi-content-specifications")).to_be_visible()
            assert fixture.count("/api/v1/products/1/content") == 1
            checks.append("lazy content object load, one request, cached tabs, ordered-list start, escaped markup, full parameters/table and separate lazy description image")
            page.set_viewport_size({"width": 390, "height": 844})
            table = page.locator(".pi-content-table-wrap")
            table.scroll_into_view_if_needed()
            assert table.evaluate("el => el.scrollWidth > el.clientWidth")
            assert page.locator("#pi-panel").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
            page.screenshot(path=str(output / "content-mobile.png"))
            page.set_viewport_size({"width": 1280, "height": 900})
            checks.append("mobile table scroll stays inside the detail panel")
            close_product()

            for identifier, expected in [("2", "部分内容"), ("3", "来源返回空内容"), ("4", "未观察到介绍字段"),
                                         ("5", "尚未采集结构化介绍"), ("6", "介绍采集尚未就绪")]:
                open_product(identifier)
                page.locator("#pi-tab-info").click()
                expect(page.locator("#pi-panel")).to_contain_text(expected)
                if identifier in {"3", "4", "5", "6"}:
                    expect(page.locator("#pi-panel")).to_contain_text("历史介绍文本")
                    expect(page.locator("#pi-panel")).to_contain_text("Legacy text <not HTML>")
                if identifier == "2":
                    expect(page.locator("#pi-panel")).to_contain_text("参数观察于 2026/10/13 12:00:00")
                    expect(page.locator("#pi-panel")).to_contain_text("正文观察于 2026/10/15 08:00:00")
                    page.locator(".pi-content-status").scroll_into_view_if_needed()
                    page.screenshot(path=str(output / "content-partial.png"))
                close_product()
            checks.append("partial, empty, missing, not-yet-collected and schema-not-ready states; explicitly labelled legacy fallback")
            checks.append("retained parameters and newer description show their own actual observation times")

            open_product("7")
            page.locator("#pi-tab-info").click()
            retry = page.locator('[data-pi-retry="content"]')
            expect(retry).to_be_visible()
            retry.click()
            expect(page.locator(".pi-content-specifications")).to_be_visible()
            assert fixture.count("/api/v1/products/7/content") == 2
            close_product()
            checks.append("503 error shows retry and a successful second content request")

            open_product("10")
            page.locator("#pi-tab-info").click()
            expect(page.locator('[data-pi-retry="content"]')).to_be_visible()
            assert page.locator(".pi-content-specifications").count() == 0
            close_product()
            checks.append("wrong-product payload is rejected")

            open_product("8")
            page.locator("#pi-tab-info").click()
            assert fixture.entered["8"].wait(3)
            close_product()
            open_product("1")
            fixture.release["8"].set()
            expect(page.locator("#dialog-title")).to_have_text("Fixture garment 1")
            expect(page.locator("#pi-tab-overview")).to_have_attribute("aria-selected", "true")
            close_product()
            checks.append("closing a pending content load aborts it and cannot replace a later product")

            page.locator('a[data-page="workbench"]').click()
            page.locator('[data-wb-drill="all"]').click()
            drill = page.locator(".catalog-list-view")
            expect(drill).to_be_visible()
            open_product("9", drill)
            page.locator("#pi-tab-info").click()
            assert fixture.entered["9"].wait(3)
            page.locator('[data-action="catalog-back"]').click()
            expect(drill).to_be_visible()
            fixture.release["9"].set()
            expect(page.locator(".catalog-count")).to_contain_text("10")
            assert page.locator("#pi-panel").count() == 0
            page.locator('[aria-label="关闭商品明细"]').click()
            checks.append("returning from a pending content load restores original drill results and discards late content")

            page.wait_for_timeout(150)
            assert not errors, errors
            assert not blocked, blocked
            for identifier in ("8", "9"):
                assert any(f"/products/{identifier}/content" in item["url"] for item in failed), failed
            checks.append("zero page errors or unsafe/unknown external requests; both pending content fetches cancelled")
            context.close()
            browser.close()
    finally:
        for event in fixture.release.values():
            event.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    report = {"status": "passed", "checks": checks, "page_errors": errors, "unexpected_requests": blocked,
              "cancelled_content_requests": [item for item in failed if "/content" in item["url"]],
              "boundary": "Real local Chromium and shipped static frontend; all APIs and image responses are in-memory fixtures; no live database or TikTok data verified."}
    (output / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8026)
    parser.add_argument("--browser-cache")
    parser.add_argument("--output", default=str(ROOT / "data" / "samples" / "frontend-content-qa"))
    run(parser.parse_args())
