"""Plan/start durable historical photo backfill; no website requests are made here."""

import argparse
import json

from app.services.image_backfill import BACKFILL_MODES, MEDIA_MODE, enqueue_image_tasks, fixed_image_seed_batch, image_backfill_status
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory, session_scope


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="创建持久化补图任务；默认仅只读显示计划。")
    parser.add_argument("--workers-ready", action="store_true", help="声明Scheduler和全部Worker已升级为支持images_only的版本。")
    parser.add_argument("--page-size", type=int, default=500, help="每页扫描/事务上限，1-1000。")
    parser.add_argument("--status", action="store_true", help="仅查询固定100店的实际图库覆盖与任务状态。")
    parser.add_argument("--mode", choices=BACKFILL_MODES, default=MEDIA_MODE, help="默认一次请求补齐相册和商品介绍。")
    args = parser.parse_args(argv)
    if not 1 <= args.page_size <= 1000:
        raise ValueError("分页范围应为1-1000。")
    if args.status and args.execute:
        raise ValueError("状态查询不能同时执行任务创建。")
    settings, batch = Settings.load(), fixed_image_seed_batch()
    if args.execute and (not getattr(settings, "image_backfill_enabled", False) or not args.workers_ready):
        raise ValueError("需显式启用图片补采，且先升级所有Worker，再使用--execute --workers-ready。")
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    try:
        if args.status:
            with factory() as session:
                print(json.dumps(image_backfill_status(session, settings, batch), ensure_ascii=False, indent=2))
            return 0
        totals = {"scanned": 0, "existing_tasks": 0, "planned_tasks": 0, "created_tasks": 0}
        cursor, pages, last = 0, 0, None
        while True:
            with session_scope(factory) if args.execute else factory() as session:
                last = enqueue_image_tasks(session, settings, batch, execute=args.execute,
                                           workers_ready=args.workers_ready, after_id=cursor, limit=args.page_size, capture_mode=args.mode)
            pages += 1
            for key in totals:
                totals[key] += last[key]
            if last["next_after_id"] is None:
                break
            cursor = last["next_after_id"]
        print(json.dumps({**last, **totals, "pages": pages, "next_after_id": None,
                          "requests_are_executed_by_existing_workers": bool(args.execute),
                          "notice": "只创建图片任务，不更改价格、SKU、基线或原全量批次；现有Worker按共享限速继续执行。"},
                         ensure_ascii=False, indent=2))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "图片补采计划或启动未完成；未输出连接信息。"}, ensure_ascii=False))
        raise SystemExit(1)
