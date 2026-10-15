"""Export a read-only monitoring view without starting collection or tasks."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from app.reports.exporter import export_report
from app.reports.summary import build_monitoring_report
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory


def main():
    parser = argparse.ArgumentParser(description="生成服装监测 HTML、Markdown 和 JSON 报告；不启动采集")
    parser.add_argument("--as-of", help="带时区的观察截止时间，默认数据库当前 UTC 时间")
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    settings = Settings.load()
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as db:
            report = build_monitoring_report(db, settings, as_of=datetime.fromisoformat(args.as_of) if args.as_of else None,
                                             lookback_hours=args.lookback_hours)
        result = export_report(report, args.output_dir or settings.report_directory)
        print(json.dumps({**result, "summary": report["summary"]}, ensure_ascii=False, indent=2))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "报表生成未完成，请检查配置与观察截止时间。"}, ensure_ascii=False))
        raise SystemExit(1)
