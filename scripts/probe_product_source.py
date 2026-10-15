"""Bounded public source probe; never copy browser cookies or request signatures."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from app.crawlers.product_list import ProductPageClient, save_product_failure
from app.crawlers.public_shop import PublicPageError, public_url, shop_id_from_url
from app.storage.database import PROJECT_ROOT


def main():
    parser = argparse.ArgumentParser(description="匿名核实店铺首屏和一次商品分页，不写数据库")
    parser.add_argument("--shop-url", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    source = public_url(args.shop_url)
    identifier = shop_id_from_url(source)
    if not identifier:
        raise ValueError("需要已核验的店铺公开入口。")
    output = args.output or PROJECT_ROOT / f"data/samples/product-discovery/probe-{uuid4().hex[:12]}.json"
    directory = output.parent / (output.stem + "-pages")
    try:
        with ProductPageClient(interval=2, max_attempts=1) as client:
            first = client.fetch_store(source)
            first_path = first.save_evidence(directory)
            report = {"shop_id": identifier, "shop_name": first.feed.shop_name, "source_url": first.final_url,
                      "first_page_products": len(first.feed.products), "has_more": first.feed.has_more,
                      "first_page_evidence": str(first_path)}
            if first.feed.next_pagination is not None:
                # Fresh session proves the continuation does not require source cookies.
                import time
                time.sleep(2)
                with ProductPageClient(interval=2, max_attempts=1) as continuation:
                    second = continuation.fetch_next(first.feed.next_pagination)
                report.update(pagination_products=len(second.feed.products), pagination_has_more=second.feed.has_more,
                              pagination_evidence=str(second.save_evidence(directory)),
                              pagination_without_browser_credentials=True, pagination_without_source_cookies=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({**report, "report": str(output)}, ensure_ascii=False, indent=2))
    except PublicPageError as error:
        save_product_failure(error, directory)
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "code": getattr(error, "code", None),
                          "status": getattr(error, "status", None)}, ensure_ascii=False))
        raise SystemExit(1)
