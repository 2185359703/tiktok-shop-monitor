"""Preview or apply the additive product review table and task type."""

import argparse
import json
import re

from sqlalchemy import CheckConstraint, inspect, text

from app.models import CrawlTask, ProductReview, TaskType
from app.storage.database import make_engine
from app.storage.schema import mysql_ddl, require_supported_server
from scripts.migrate_shop_profiles import TASK_CHECK, add_check, column_differences


LOCK_NAME = "tiktok_product_reviews_migration"


def review_ddl():
    ddl = mysql_ddl(table_names=(ProductReview.__tablename__,))
    return ddl[ddl.index("CREATE TABLE IF NOT EXISTS "):]


def additive_sql():
    return ("-- Add public reviews and allow their independent coroutine task.\n"
            "-- Repeated migration: python -m scripts.migrate_product_reviews --execute\n"
            "SET NAMES utf8mb4;\nSET time_zone = '+00:00';\n\n" + review_ddl()
            + f"\nALTER TABLE crawl_tasks DROP CHECK `{TASK_CHECK}`, "
            + add_check(CrawlTask.__table__, TASK_CHECK) + ";\n")


def migration_plan(connection):
    inspector = inspect(connection)
    names = set(inspector.get_table_names())
    if not {"products", "crawl_tasks"}.issubset(names):
        raise ValueError("需要先初始化商品和任务表。")
    checks = {item["name"]: item["sqltext"] for item in inspector.get_check_constraints("crawl_tasks")}
    allowed = set(re.findall(r"'([^']+)'", checks.get(TASK_CHECK, "")))
    expected = {kind.value for kind in TaskType}
    if not allowed or not allowed.issubset(expected):
        raise ValueError("任务类型约束缺失或包含未知类型。")
    actions = []
    if ProductReview.__tablename__ in names:
        table = ProductReview.__table__
        columns = {c["name"]: c for c in inspector.get_columns(table.name)}
        differences = column_differences(columns, table.c, connection.dialect)
        unique = {tuple(c["column_names"]) for c in inspector.get_unique_constraints(table.name)}
        if ("product_id", "platform_review_id") not in unique:
            differences.append("product_reviews: identity constraint missing")
        keys = {(tuple(k["constrained_columns"]), k["referred_table"], tuple(k["referred_columns"]))
                for k in inspector.get_foreign_keys(table.name)}
        for key in ((("product_id",), "products", ("id",)), (("task_id",), "crawl_tasks", ("id",))):
            if key not in keys:
                differences.append("product_reviews: foreign key missing")
        constraints = {c["name"] for c in inspector.get_check_constraints(table.name)}
        if any(c.name not in constraints for c in table.constraints if isinstance(c, CheckConstraint)):
            differences.append("product_reviews: check constraint missing")
        if ("product_id", "id") not in {tuple(i["column_names"]) for i in inspector.get_indexes(table.name)}:
            differences.append("product_reviews: pagination index missing")
        if differences:
            raise ValueError("; ".join(differences))
    else:
        actions.extend(s.strip() for s in review_ddl().split(";") if s.strip())
    if allowed != expected:
        actions.append(f"ALTER TABLE crawl_tasks DROP CHECK `{TASK_CHECK}`, " + add_check(CrawlTask.__table__, TASK_CHECK))
    return actions


def migrate(engine, *, execute=False):
    require_supported_server(engine)
    with engine.connect() as connection:
        if execute and connection.scalar(text("SELECT GET_LOCK(:name, 0)"), {"name": LOCK_NAME}) != 1:
            raise ValueError("已有评论迁移在执行。")
        try:
            actions = migration_plan(connection)
            if execute:
                for statement in actions:
                    connection.exec_driver_sql(statement)
            remaining = migration_plan(connection) if execute else actions
            return {"mode": "execute" if execute else "check", "ready": not remaining,
                    "executed_statements": len(actions) if execute else 0,
                    "pending_statements": len(remaining), "data_rows_modified": 0}
        finally:
            if execute:
                connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": LOCK_NAME})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--execute", action="store_true")
    modes.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute and not args.check:
        print(additive_sql(), end="")
        return 0
    engine = make_engine()
    try:
        result = migrate(engine, execute=args.execute)
        print(json.dumps(result))
        return 0 if result["ready"] else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "评论迁移未完成，请核查表结构。"}, ensure_ascii=False))
        raise SystemExit(1)
