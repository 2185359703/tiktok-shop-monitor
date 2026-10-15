"""Read one public gallery to verify image extraction, without any database access."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from app.crawlers.product_detail import ProductDetailClient
from app.crawlers.product_images import IMAGE_SOURCE_VERSION
from app.crawlers.public_shop import PublicPageError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product-url", required=True)
    parser.add_argument("--expected-shop-id", required=True)
    parser.add_argument("--http-backend", choices=("httpx", "curl_cffi"), default="httpx")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        with ProductDetailClient(interval=2, max_attempts=1, http_backend=args.http_backend) as client:
            snapshot = client.fetch_detail(args.product_url, args.expected_shop_id)
        record = {"source_url": snapshot.final_url, "source_version": IMAGE_SOURCE_VERSION,
                  "response_hash": snapshot.response_hash, "observed_at": snapshot.captured_at.isoformat(),
                  "platform_product_id": snapshot.detail.platform_product_id,
                  "images": [asdict(image) for image in snapshot.detail.images]}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"platform_product_id": snapshot.detail.platform_product_id,
                          "images": len(snapshot.detail.images), "response_hash": snapshot.response_hash,
                          "observed_at": snapshot.captured_at.isoformat(), "database_access": False}))
        return 0 if snapshot.detail.images else 1
    except PublicPageError as error:
        print(json.dumps({"code": error.code, "status": error.status, "database_access": False}))
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "公开图片取样未完成。"}, ensure_ascii=False))
        raise SystemExit(1)
