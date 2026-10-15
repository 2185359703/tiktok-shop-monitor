"""Measure real, read-only analytics with a per-query execution bound."""

import argparse
import json
import sys
import time

from sqlalchemy import event, text

from app.services.analytics_service import build_workbench, filters, workbench_variant_query
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory


def peak_rss_mib():
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        *[(name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
                            "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                            "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        current = ctypes.windll.kernel32.GetCurrentProcess
        current.restype = wintypes.HANDLE
        read = ctypes.windll.psapi.GetProcessMemoryInfo
        read.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not read(current(), ctypes.byref(counters), counters.cb):
            raise OSError("Process memory measurement failed.")
        return round(counters.PeakWorkingSetSize / (1024 ** 2), 2)
    import resource
    scale = 1024 ** 2 if sys.platform == "darwin" else 1024
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale, 2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shop-id", type=int)
    parser.add_argument("--query-timeout-ms", type=int, default=10000)
    parser.add_argument("--sku-plan-only", action="store_true")
    args = parser.parse_args()
    if not 1000 <= args.query_timeout_ms <= 30000 or args.shop_id is not None and args.shop_id <= 0:
        raise ValueError("诊断范围或超时不正确。")
    config, timings = Settings.load(), []
    engine = make_engine(config.database_url)
    # build_workbench owns an additional RC connection; bound every connection,
    # rather than setting a limit only on the unused caller transaction.
    def configure_connection(dbapi_connection, record):
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET SESSION MAX_EXECUTION_TIME=%s", (args.query_timeout_ms,))
    event.listen(engine, "connect", configure_connection)
    def before(connection, cursor, statement, parameters, context, executemany):
        context._profile_began = time.monotonic()
    def after(connection, cursor, statement, parameters, context, executemany):
        timings.append(round(time.monotonic() - context._profile_began, 4))
    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    began = time.monotonic()
    try:
        if args.sku_plan_only:
            with make_session_factory(engine)() as db:
                clauses, scope = filters(config, session=db, shop_id=args.shop_id)
                with workbench_variant_query(db, clauses, scope["currency"]) as query:
                    sql = str(query.compile(dialect=engine.dialect, compile_kwargs={"literal_binds": True}))
                    plan = db.execute(text("EXPLAIN " + sql)).mappings().all()
                    print(json.dumps({"stage": "reference_sku_select_plan", "business_data_read_only": True,
                        "plan": [dict(row) for row in plan]}, ensure_ascii=False, default=str, indent=2))
            return
        with make_session_factory(engine)() as db:
            data = build_workbench(db, config, shop_id=args.shop_id)
        print(json.dumps({"business_data_read_only": True, "elapsed_seconds": round(time.monotonic() - began, 3),
            "peak_rss_mib": peak_rss_mib(),
            "summary": data["summary"], "queries": len(timings), "query_seconds": timings,
            "generated_at": data["generated_at"]}, ensure_ascii=False, indent=2))
    except Exception as error:
        original = getattr(getattr(error, "orig", None), "args", ())
        print(json.dumps({"business_data_read_only": True, "error_type": type(error).__name__,
                          "database_error_code": original[0] if original else None,
                          "completed_queries": len(timings), "query_seconds": timings,
                          "elapsed_seconds": round(time.monotonic() - began, 3)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
