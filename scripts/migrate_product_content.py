"""Preview or create only product_contents, without altering catalog tables."""

import argparse
import json

from sqlalchemy import inspect

from app.models import Base
from app.storage.database import configured_database_url, make_engine
from app.storage.schema import INITIAL_TABLE_NAMES, model_tables, mysql_ddl, require_supported_server, schema_drift


CONTENT_TABLE_NAMES = ("product_contents",)


def additive_sql():
    ddl = mysql_ddl(table_names=CONTENT_TABLE_NAMES)
    return "-- Additive merchant content storage. Existing tables and data are unchanged.\nSET NAMES utf8mb4;\nSET time_zone = '+00:00';\n\n" + ddl[ddl.index("CREATE TABLE IF NOT EXISTS "):]


def schema_status(engine):
    existing = set(inspect(engine).get_table_names())
    missing_initial = sorted(set(INITIAL_TABLE_NAMES) - existing)
    differences = schema_drift(engine)
    present = "product_contents" in existing
    return {"tables": {"product_contents": "present" if present else "missing"},
            "initial_tables_missing": missing_initial, "schema_drift": differences,
            "ready": present and not missing_initial and not differences}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--execute", action="store_true")
    modes.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute and not args.check:
        print(json.dumps({"mode": "dry_run", "tables": list(CONTENT_TABLE_NAMES),
                          "database_connections": 0, "catalog_table_alterations": 0, "data_writes": 0}))
        print(additive_sql(), end="")
        return 0
    engine = make_engine(configured_database_url())
    try:
        require_supported_server(engine)
        before = schema_status(engine)
        if args.check:
            print(json.dumps({"mode": "check", **before}, ensure_ascii=False, indent=2))
            return 0 if before["ready"] else 1
        if before["initial_tables_missing"] or before["schema_drift"]:
            raise ValueError("现有基础表缺失或定义不一致，本命令不会修复旧表。")
        Base.metadata.create_all(engine, tables=model_tables(CONTENT_TABLE_NAMES), checkfirst=True)
        after = schema_status(engine)
        print(json.dumps({"mode": "execute", "created_tables": ["product_contents"] if before["tables"]["product_contents"] == "missing" and after["ready"] else [],
                          "catalog_table_alterations": 0, "data_writes": 0, **after}, ensure_ascii=False, indent=2))
        return 0 if after["ready"] else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "商品介绍扩展表操作未完成；未修改旧表与采集任务。"}, ensure_ascii=False))
        raise SystemExit(1)
