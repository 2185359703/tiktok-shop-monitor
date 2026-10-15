"""Measure fixed-scope progress without printing credentials or large rows."""

import argparse
import json
import runpy
import time
from pathlib import Path

from sqlalchemy import text

from app.services.collection_service import collection_progress
from app.services.seed_importer import read_seed_csv
from app.settings import Settings
from app.storage.database import PROJECT_ROOT, make_engine, make_session_factory


def main():
    parser = argparse.ArgumentParser(description="只读计时固定名单统计，不修改采集任务")
    parser.add_argument("--candidate", type=Path, help="显式读取本次审核的统计实现，供上线前对比")
    args = parser.parse_args()
    progress = runpy.run_path(str(args.candidate))["collection_progress"] if args.candidate else collection_progress
    config = Settings.load()
    engine = make_engine(config.database_url)
    try:
        began = time.monotonic()
        with make_session_factory(engine)() as db:
            db.execute(text("SET SESSION MAX_EXECUTION_TIME=10000"))
            state = progress(db, read_seed_csv(PROJECT_ROOT / "data/seed/shops_us_clothing.csv"),
                             require_baseline=config.baseline_on_detail)
        state.pop("shops")
        print(json.dumps({"read_only": True, "elapsed_seconds": round(time.monotonic() - began, 3),
                          "progress": state}, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "限时统计未完成，未修改任务或输出凭据。"}, ensure_ascii=False))
        raise SystemExit(1)
