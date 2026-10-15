"""Read only public PDP business fields, excluding reviews and AI summaries."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from app.crawlers.product_list import product_id_from_url, save_product_failure
from app.crawlers.product_detail import ProductDetailClient, PARSER_VERSION
from app.crawlers.public_shop import PublicPageError, public_url
from app.storage.database import PROJECT_ROOT


def main():
    parser = argparse.ArgumentParser(description="公开商品详情结构取样，不写数据库")
    parser.add_argument("--product-url", required=True)
    parser.add_argument("--expected-shop-id", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    source = public_url(args.product_url)
    product_id = product_id_from_url(source)
    if product_id is None:
        raise ValueError("需要有效的美国站商品公开入口。")
    try:
        with ProductDetailClient(interval=2, max_attempts=1) as client:
            snapshot = client.fetch_detail(source, args.expected_shop_id)
        output = args.output or PROJECT_ROOT / f"data/samples/product-detail/source-{uuid4().hex[:12]}.json"
        evidence = snapshot.save_evidence(output.parent / (output.stem + "-pages"))
        report = {"product_id": product_id, "shop_id": snapshot.detail.platform_shop_id,
                  "sku_count": len(snapshot.detail.variants), "response_hash": snapshot.response_hash,
                  "attribute_conflicts": len(snapshot.detail.raw_attributes["attribute_conflicts"]), "evidence": str(evidence)}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"shop_id": args.expected_shop_id, "product_id": product_id,
                          "sku_count": len(snapshot.detail.variants), "report": str(output)}, ensure_ascii=False))
    except PublicPageError as error:
        path = save_product_failure(error, PROJECT_ROOT / "data/samples/product-detail/probe-failures", parser_version=PARSER_VERSION)
        print(json.dumps({"code": error.code, "status": error.status, "evidence": str(path)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "公开详情取样未完成。"}, ensure_ascii=False))
        raise SystemExit(1)
