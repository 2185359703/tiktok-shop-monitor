"""Measure real, read-only analytics with a per-query execution bound."""

import argparse
import json
import resource
import time

from sqlalchemy import event

from app.services.analytics_service import build_workbench
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shop-id", type=int)
    parser.add_argument("--query-timeout-ms", type=int, default=10000)
    args = parser.parse_args()
    if not 1000 <= args.query_timeout_ms <= 120000 or args.shop_id is not None and args.shop_id <= 0:
        raise ValueError("诊断范围或超时不正确。")
    config, timings = Settings.load(), []
    engine = make_engine(config.database_url)
    # build_workbench owns an additional RC connection; bound every connection,
    # rather than setting a limit only on the unused caller transaction.
    def configure_connection(dbapi_connection, record):
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET SESSION MAX_EXECUTION_TIME=%s", (args.query_timeout_ms,))
    event.listen(engine, "connect", configure_connection)
    last_statement = {}
    def before(connection, cursor, statement, parameters, context, executemany):
        context._profile_began = time.monotonic()
        last_statement.update(action=statement.lstrip().split()[0], temporary="workbench_skus_" in statement,
                              grouped="GROUP BY" in statement, window="OVER (" in statement)
    def after(connection, cursor, statement, parameters, context, executemany):
        timings.append(round(time.monotonic() - context._profile_began, 4))
    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    began = time.monotonic()
    try:
        with make_session_factory(engine)() as db:
            data = build_workbench(db, config, shop_id=args.shop_id)
        print(json.dumps({"business_data_read_only": True, "elapsed_seconds": round(time.monotonic() - began, 3),
            "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2),
            "summary": data["summary"], "queries": len(timings), "slowest_query_seconds": sorted(timings, reverse=True)[:8],
            "generated_at": data["generated_at"]}, ensure_ascii=False, indent=2))
    except Exception as error:
        original = getattr(getattr(error, "orig", None), "args", ())
        print(json.dumps({"business_data_read_only": True, "error_type": type(error).__name__,
                          "database_error_code": original[0] if original else None,
                          "completed_queries": len(timings), "slowest_query_seconds": sorted(timings, reverse=True)[:8],
                          "last_statement": last_statement,
                          "elapsed_seconds": round(time.monotonic() - began, 3)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
