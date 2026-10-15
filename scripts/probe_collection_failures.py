"""Extract only public business URL/ID diagnostics from a few failed sources."""

import argparse
import json
import re

from app.crawlers.public_shop import PublicShopClient, RouterScriptParser, public_url
from app.crawlers.product_list import product_id_from_url


def main():
    parser = argparse.ArgumentParser(description="少量公开入口取样，只输出商品 ID/规范链接与页面结构")
    parser.add_argument("--url", action="append", required=True)
    args = parser.parse_args()
    if len(args.url) > 3: raise ValueError("诊断取样最多三个公开入口。")
    results = []
    with PublicShopClient(max_attempts=1) as client:
        for url in args.url:
            final, body, encoding = client._read(public_url(url))
            document = body.decode(encoding, errors="replace")
            title = re.search(r"<title[^>]*>(.*?)</title>", document, re.I | re.S)
            script = RouterScriptParser(); script.feed(document)
            item = {"url": final, "bytes": len(body), "title": title.group(1)[:200] if title else None,
                    "router_script_count": script.script_count, "mismatches": []}
            if script.parts:
                data = json.loads("".join(script.parts))
                for route, page in data.get("loaderData", {}).items():
                    if not isinstance(page, dict) or not route.endswith("/page"): continue
                    components = (page.get("page_config") or {}).get("components_map", [])
                    entries = list(components.values()) if isinstance(components, dict) else components
                    item["component_types"] = [entry.get("component_type") for entry in entries]
                    for entry in entries:
                        if entry.get("component_type") != "feed_list": continue
                        for product in entry.get("component_data", {}).get("products", []):
                            pid, seo = str(product.get("product_id")), (product.get("seo_url") or {}).get("canonical_url")
                            if product_id_from_url(seo) != pid:
                                item["mismatches"].append({"product_id": pid, "canonical_url": seo,
                                    "url_id": product_id_from_url(seo), "seller_id": str((product.get("seller_info") or {}).get("seller_id"))})
            results.append(item)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
