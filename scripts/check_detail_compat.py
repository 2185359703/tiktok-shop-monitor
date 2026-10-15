"""Verify the actual configured client against one persisted public task."""

import argparse
import json
from threading import Event

from sqlalchemy import select

from app.crawlers.product_detail import ProductDetailClient
from app.models import CrawlTask, TaskType
from app.queue.redis_gate import RedisGate
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory


def main():
    parser = argparse.ArgumentParser(description="只读核验最终详情客户端，最多三个独立匿名会话")
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    config = Settings.load()
    engine, gate, stop = make_engine(config.database_url), RedisGate(config), Event()
    try:
        with make_session_factory(engine)() as db:
            task = db.scalar(select(CrawlTask).where(CrawlTask.task_id == args.task_id, CrawlTask.platform == config.platform,
                CrawlTask.region == config.region, CrawlTask.task_type == TaskType.PRODUCT_DETAIL))
            if task is None: raise ValueError("任务不属于当前公开详情采集范围。")
            url, shop_id, product_id = (task.payload[key] for key in ("product_url", "platform_shop_id", "platform_product_id"))
        records = []
        for attempt in range(3):
            try:
                with ProductDetailClient(interval=0, max_attempts=1, http_backend=config.detail_http_backend,
                                         http_proxy=config.detail_proxy_url, before_request=lambda: gate.wait_turn(stop)) as client:
                    result = client.fetch_detail(url, shop_id)
                    if result.detail.platform_product_id != product_id: raise ValueError("详情身份不一致。")
                    records.append({"accepted": True, "product_id": product_id, "shop_id": shop_id,
                                    "sku_count": len(result.detail.variants), "currency": result.detail.currency})
            except Exception as error:
                records.append({"accepted": False, "error_type": type(error).__name__, "failure_code": getattr(error, "code", None)})
        print(json.dumps({"task_id": args.task_id, "backend": config.detail_http_backend,
                          "proxy_configured": bool(config.detail_proxy_url), "browser_state_copied": False,
                          "database_written": False, "records": records}, ensure_ascii=False, indent=2))
    finally:
        gate.close()
        engine.dispose()


if __name__ == "__main__":
    main()
