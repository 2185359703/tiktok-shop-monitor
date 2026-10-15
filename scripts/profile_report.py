"""Read one actual report, returning only coverage, elapsed time and peak RSS."""

import json
import resource
import time

from app.reports.summary import build_monitoring_report
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory


def main():
    config = Settings.load()
    engine = make_engine(config.database_url)
    try:
        started = time.monotonic()
        with make_session_factory(engine)() as db:
            report = build_monitoring_report(db, config)
        print(json.dumps({"read_only": True, "as_of": report["as_of"],
                          "elapsed_seconds": round(time.monotonic() - started, 3),
                          "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2),
                          "summary": report["summary"]}, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "只读报告核验未完成，未输出连接或原始负载。"}, ensure_ascii=False))
        raise SystemExit(1)
