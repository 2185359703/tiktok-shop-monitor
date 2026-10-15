"""Verify deployed static bytes and public read APIs without website collection."""

import argparse
import hashlib
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18080")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--static-only", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    def read(path):
        with urllib.request.urlopen(args.url + path, timeout=60) as response:
            return response.read()
    def asset(record):
        name = record["path"][len("app/frontend/"):]
        path = "/" if name == "index.html" else "/ui-assets/" + name
        body = read(path)
        matched = hashlib.sha256(body).hexdigest() == record["sha256"]
        return {"path": path, "bytes": len(body), "sha256_matches": matched}
    records = [record for record in manifest["files"] if record["path"].startswith("app/frontend/")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assets = list(pool.map(asset, records))
    if args.static_only:
        result = {"release_id": manifest["release_id"], "url": args.url, "static_assets": assets,
                  "all_static_assets_match": all(record["sha256_matches"] for record in assets),
                  "read_apis_checked": []}
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"static_assets": len(assets), "all_static_assets_match": result["all_static_assets_match"]}))
        if not result["all_static_assets_match"]:
            raise SystemExit(1)
        return
    paths = ("/health/ready", "/api/v1/capabilities", "/api/v1/images/backfill/status",
             "/api/v1/analytics/products?page_size=2", "/api/v1/matches/summary")
    with ThreadPoolExecutor(max_workers=3) as pool:
        data = dict(zip(paths, pool.map(lambda path: json.loads(read(path)), paths)))
    config = data["/api/v1/capabilities"]
    photos = data["/api/v1/images/backfill/status"]
    result = {"release_id": manifest["release_id"], "url": args.url,
              "all_static_assets_match": all(record["sha256_matches"] for record in assets),
              "static_assets": assets, "read_apis_checked": list(paths),
              "product_detail_capture_modes": config.get("product_detail_capture_modes"),
              "image_backfill_enabled": config.get("image_backfill_enabled"),
              "image_backfill": photos,
              "product_total": data["/api/v1/analytics/products?page_size=2"].get("total")}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"release_id": result["release_id"], "static_assets": len(assets),
        "all_static_assets_match": result["all_static_assets_match"], "read_apis_checked": len(paths),
        "product_total": result["product_total"], "image_tasks": photos.get("tasks_total"),
        "image_products": photos.get("products_with_current_images")}, ensure_ascii=False))
    if not result["all_static_assets_match"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
