"""Capture a small allowlisted public product projection for parser diagnosis."""

import argparse
import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import Event

from sqlalchemy import select

from app.crawlers.public_shop import PublicShopClient, RouterScriptParser
from app.models import CrawlTask, TaskType
from app.queue.redis_gate import RedisGate
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory


def main():
    parser = argparse.ArgumentParser(description="最多三个现有任务的公开规格取样；共享现有请求闸门")
    parser.add_argument("--task-id", action="append", required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    if len(args.task_id) > 3:
        raise ValueError("一次最多三个任务。")
    config = Settings.load()
    engine, gate, stop = make_engine(config.database_url), RedisGate(config), Event()
    try:
        with make_session_factory(engine)() as db:
            tasks = db.scalars(select(CrawlTask).where(CrawlTask.task_id.in_(args.task_id),
                CrawlTask.platform == config.platform, CrawlTask.region == config.region,
                CrawlTask.task_type == TaskType.PRODUCT_DETAIL)).all()
            if len(tasks) != len(set(args.task_id)):
                raise ValueError("任务不是当前市场的详情任务。")
        args.output_directory.mkdir(parents=True, exist_ok=True)
        results = []
        for task in tasks:
            with PublicShopClient(interval=0, max_attempts=1, http_backend=config.detail_http_backend,
                                  http_proxy=config.detail_proxy_url, before_request=lambda: gate.wait_turn(stop)) as client:
                final, body, encoding = client._read(task.payload["product_url"])
            script = RouterScriptParser()
            script.feed(body.decode(encoding, errors="replace"))
            record = {"task_id": task.task_id, "captured_at": datetime.now(timezone.utc).isoformat(),
                      "final_url": final, "http_status": 200, "response_hash": sha256(body).hexdigest(),
                      "expected_product_id": task.payload["platform_product_id"],
                      "expected_shop_id": task.payload["platform_shop_id"], "blocks": []}
            if script.script_count == 1 and script.parts:
                root = json.loads("".join(script.parts))
                for route, page in root.get("loaderData", {}).items():
                    if not route.endswith("/page") or not isinstance(page, dict):
                        continue
                    components = (page.get("page_config") or {}).get("components_map") or []
                    entries = components.values() if isinstance(components, dict) else components
                    for entry in entries:
                        if entry.get("component_type") != "product_info":
                            continue
                        block = entry["component_data"]
                        info, shop = block.get("product_info") or {}, block.get("shop_info") or {}
                        projection = {key: info.get(key) for key in ("product_id", "seller_id", "status", "price", "sale_props")}
                        projection["skus"] = [{key: row.get(key) for key in
                            ("sku_id", "status", "sku_sale_props", "price", "stock", "pdp_button_area_id")}
                            for row in info.get("skus") or []]
                        record["blocks"].append({"error_code": block.get("error_code"),
                            "shop_info": {key: shop.get(key) for key in ("seller_id", "region")}, "product_info": projection})
            path = args.output_directory / f"{task.task_id}.json"
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            results.append({"task_id": task.task_id, "projected_blocks": len(record["blocks"]),
                            "sku_count": sum(len(block["product_info"]["skus"]) for block in record["blocks"]),
                            "evidence_path": str(path)})
        print(json.dumps({"database_written": False, "browser_state_copied": False, "samples": results},
                         ensure_ascii=False, indent=2))
    finally:
        gate.close()
        engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "failure_code": getattr(error, "code", None),
                          "message": "取样未完成；未保存完整页面、会话或连接信息。"}, ensure_ascii=False))
        raise SystemExit(1)
