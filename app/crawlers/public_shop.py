"""Read public TikTok Shop SSR data without login or signed API replay."""

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


PARSER_VERSION = "public_shop_ssr_1"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class PublicPageError(Exception):
    def __init__(self, code: str, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable


def public_url(value: str) -> str:
    """Only known public US shopping routes; strip tracking/signature parameters."""
    try:
        parsed = urlsplit(value)
        allowed = (
            parsed.hostname == "shop.tiktok.com"
            and re.match(r"^/us/(?:k|c|store|pdp)/[^/]+", parsed.path)
        ) or (
            parsed.hostname == "www.tiktok.com"
            and re.match(r"^/shop/store/[^/]+/\d+/?$", parsed.path)
        )
        if (
            parsed.scheme != "https" or not allowed or parsed.port not in {None, 443}
            or parsed.username is not None or parsed.password is not None
            or any(character.isspace() for character in value)
        ):
            raise ValueError
    except ValueError:
        raise PublicPageError("unsupported_source", "仅支持无凭据的美国站 TikTok Shop 公开入口。") from None
    return urlunsplit(("https", parsed.hostname, parsed.path, "", ""))


def shop_id_from_url(value: str) -> str | None:
    try:
        cleaned = public_url(value)
    except PublicPageError:
        return None
    match = re.fullmatch(r"/(?:us|shop)/store/[^/]+/(\d+)/?", urlsplit(cleaned).path)
    return match.group(1) if match else None


@dataclass(frozen=True)
class ShopCandidate:
    platform_shop_id: str
    canonical_name: str
    observed_names: tuple[str, ...]
    canonical_url: str | None
    region: str
    evidence_paths: tuple[str, ...]


@dataclass(frozen=True)
class PublicPageSnapshot:
    source_url: str
    final_url: str
    status: int
    response_bytes: int
    response_hash: str
    latency_ms: int
    network_attempts: int
    started_at: datetime
    captured_at: datetime
    candidates: tuple[ShopCandidate, ...]

    @classmethod
    def from_evidence(cls, record: dict) -> "PublicPageSnapshot":
        return cls(
            record["source_url"], record["final_url"], record["status"], record["response_bytes"],
            record["response_hash"], record["latency_ms"], record["network_attempts"],
            datetime.fromisoformat(record["started_at"]), datetime.fromisoformat(record["captured_at"]),
            tuple(ShopCandidate(
                item["platform_shop_id"], item["canonical_name"], tuple(item["observed_names"]),
                item["canonical_url"], item["region"], tuple(item["evidence_paths"]),
            ) for item in record["candidates"]),
        )

    def save_evidence(self, directory: Path) -> Path:
        """Persist allowlisted business facts, not HTML, cookies or router layout."""
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.response_hash}.json"
        record = asdict(self)
        record.update(parser_version=PARSER_VERSION, evidence_kind="public_shop_identity_projection")
        record["started_at"] = self.started_at.isoformat()
        record["captured_at"] = self.captured_at.isoformat()
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path


class RouterScriptParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts: list[str] = []
        self.script_count = 0

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__MODERN_ROUTER_DATA__":
            self.script_count += 1
            self.active = True

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False


def parse_shop_candidates(html: str, source_url: str) -> tuple[ShopCandidate, ...]:
    """Extract only business components, deduping seller references by stable ID."""
    source_url = public_url(source_url)
    if re.search(r"<title[^>]*>\s*Security\s+Check\s*</title>", html, flags=re.IGNORECASE):
        raise PublicPageError("access_limited", "页面返回安全验证，停止本批访问。", status=200)
    parser = RouterScriptParser()
    parser.feed(html)
    if parser.script_count != 1 or not parser.parts:
        raise PublicPageError("missing_router_data", "页面没有有效 SSR 数据；可能是访问限制或结构变化。")
    try:
        data = json.loads("".join(parser.parts))
    except (ValueError, RecursionError):
        raise PublicPageError("invalid_router_data", "页面 SSR JSON 解析失败。") from None
    loaders = data.get("loaderData") if isinstance(data, dict) else None
    if not isinstance(loaders, dict):
        raise PublicPageError("invalid_router_data", "页面缺少 loaderData。")
    candidates: dict[str, dict] = {}
    component_found = False
    for route, page in loaders.items():
        if not isinstance(page, dict) or not str(route).endswith("/page"):
            continue
        configuration = page.get("page_config")
        components = configuration.get("components_map") if isinstance(configuration, dict) else None
        if not isinstance(components, (dict, list)):
            continue
        route_info = page.get("route_info", {})
        if isinstance(route_info, dict) and str(route_info.get("path_region") or "us").lower() != "us":
            raise PublicPageError("market_mismatch", "页面 SSR 市场与美国站入口不一致。")
        component_found = True
        stack = [(components, f"loaderData.{route}.page_config.components_map", 0)]
        while stack:
            value, path, depth = stack.pop()
            if depth > 40:
                raise PublicPageError("invalid_router_data", "页面业务数据嵌套层级异常。")
            if isinstance(value, dict):
                identifier = value.get("seller_id")
                name = value.get("shop_name")
                # Python parses large JSON integers losslessly. Floats are not IDs.
                if type(identifier) is int:
                    identifier = str(identifier)
                if (
                    isinstance(identifier, str) and re.fullmatch(r"\d{1,128}", identifier)
                    and isinstance(name, str) and 0 < len(name.strip()) <= 255
                    and value.get("region", "US") == "US"
                ):
                    name = name.strip()
                    url = None
                    raw_url = value.get("shop_link")
                    if isinstance(raw_url, str) and shop_id_from_url(raw_url) == identifier:
                        url = public_url(raw_url)
                    if shop_id_from_url(source_url) == identifier:
                        url = source_url
                    candidate = candidates.setdefault(identifier, {
                        "name": name, "names": set(), "url": None, "paths": set(),
                    })
                    candidate["names"].add(name)
                    candidate["paths"].add(path)
                    if url:
                        candidate.update(name=name, url=url)
                for key, child in reversed(list(value.items())):
                    if isinstance(child, (dict, list)):
                        stack.append((child, f"{path}.{key}", depth + 1))
            elif isinstance(value, list):
                for index in range(len(value) - 1, -1, -1):
                    stack.append((value[index], f"{path}.{index}", depth + 1))
    if not component_found:
        raise PublicPageError("missing_components", "页面缺少已验证的业务组件，停止猜测字段。")
    return tuple(ShopCandidate(
        identifier, value["name"], tuple(sorted(value["names"])), value["url"], "US",
        tuple(sorted(value["paths"])),
    ) for identifier, value in sorted(candidates.items()))


class PublicShopClient:
    """Sequential, bounded public reads. 403/429 do not trigger evasion or loops."""
    def __init__(self, *, interval: float = 2.0, max_attempts: int = 3, transport=None, before_request=None, http_backend="httpx", http_proxy=None):
        if interval < 0 or not 1 <= max_attempts <= 3:
            raise ValueError("interval 必须非负，max_attempts 必须在 1-3 之间。")
        self.interval = interval
        self.max_attempts = max_attempts
        self.before_request = before_request
        self.last_request_finished = 0.0
        if http_backend == "curl_cffi":
            if transport is not None: raise ValueError("curl_cffi 不接受 HTTPX transport。")
            from app.crawlers.http_transport import CurlHTTPClient
            self.client = CurlHTTPClient(proxy=http_proxy)
        elif http_backend == "httpx":
            self.client = httpx.Client(
                timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=False,
                headers={"User-Agent": "tiktok-shop-monitor/0.1 (public-source-discovery)",
                         "Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"},
                transport=transport,
            )
        else:
            raise ValueError("公开 HTTP 客户端类型未知。")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.client.close()

    def before_http_request(self) -> None:
        delay = self.interval - (time.monotonic() - self.last_request_finished)
        if delay > 0:
            time.sleep(delay)
        if self.before_request is not None:
            self.before_request()

    def _read(self, url: str) -> tuple[str, bytes, str]:
        for hop in range(4):
            self.before_http_request()
            try:
                with self.client.stream("GET", url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location or hop == 3:
                            raise PublicPageError("redirect_error", "公开入口重定向异常。")
                        url = public_url(urljoin(url, location))
                        continue
                    if response.status_code in {401, 403, 429}:
                        raise PublicPageError("access_limited", "公开入口要求认证或被限流；停止本批访问。",
                                              status=response.status_code)
                    if response.status_code != 200:
                        raise PublicPageError("http_error", "公开入口返回非成功响应。",
                                              status=response.status_code, retryable=response.status_code >= 500)
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise PublicPageError("response_too_large", "响应超过 8 MiB，停止读取。", status=200)
                        chunks.append(chunk)
                    return url, b"".join(chunks), response.encoding or "utf-8"
            finally:
                self.last_request_finished = time.monotonic()
        raise PublicPageError("redirect_error", "公开入口重定向次数过多。")

    def fetch(self, source_url: str) -> PublicPageSnapshot:
        source_url = public_url(source_url)
        began = datetime.now(timezone.utc)
        start = time.monotonic()
        for attempt in range(1, self.max_attempts + 1):
            try:
                final_url, body, encoding = self._read(source_url)
                try:
                    candidates = parse_shop_candidates(body.decode(encoding, errors="replace"), final_url)
                except PublicPageError as exc:
                    exc.status = 200
                    raise
                return PublicPageSnapshot(
                    source_url, final_url, 200, len(body), sha256(body).hexdigest(),
                    round((time.monotonic() - start) * 1000), attempt, began,
                    datetime.now(timezone.utc), candidates,
                )
            except httpx.RequestError:
                failure = PublicPageError("network_error", "公开入口连接失败或超时。", retryable=True)
            except PublicPageError as exc:
                failure = exc
            if not failure.retryable or attempt == self.max_attempts:
                raise failure from None
            time.sleep(min(2 ** attempt, 4))
        raise AssertionError("unreachable")
