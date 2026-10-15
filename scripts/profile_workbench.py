"""Measure real, read-only analytics with a per-query execution bound."""

import argparse
import json
import resource
import time

from sqlalchemy import event, text

from app.services.analytics_service import build_workbench
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shop-id", type=int)
    parser.add_argument("--query-timeout-ms", type=int, default=10000)
    args = parser.parse_args()
    if not 1000 <= args.query_timeout_ms <= 30000 or args.shop_id is not None and args.shop_id <= 0:
        raise ValueError("诊断范围或超时不正确。")
    config, timings = Settings.load(), []
    engine = make_engine(config.database_url)
    def before(connection, cursor, statement, parameters, context, executemany):
        context._profile_began = time.monotonic()
    def after(connection, cursor, statement, parameters, context, executemany):
        timings.append(round(time.monotonic() - context._profile_began, 4))
    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    began = time.monotonic()
    try:
        with make_session_factory(engine)() as db:
            db.execute(text("SET SESSION MAX_EXECUTION_TIME=:milliseconds"), {"milliseconds": args.query_timeout_ms})
            data = build_workbench(db, config, shop_id=args.shop_id)
        print(json.dumps({"read_only": True, "elapsed_seconds": round(time.monotonic() - began, 3),
            "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2),
            "summary": data["summary"], "queries": len(timings), "query_seconds": timings,
            "generated_at": data["generated_at"]}, ensure_ascii=False, indent=2))
    except Exception as error:
        original = getattr(getattr(error, "orig", None), "args", ())
        print(json.dumps({"read_only": True, "error_type": type(error).__name__,
                          "database_error_code": original[0] if original else None,
                          "completed_queries": len(timings), "query_seconds": timings,
                          "elapsed_seconds": round(time.monotonic() - began, 3)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
