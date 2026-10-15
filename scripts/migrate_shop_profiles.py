"""Preview or apply the additive shop profile schema migration."""

import argparse
import json
import re

from sqlalchemy import CheckConstraint, inspect, text
from sqlalchemy.dialects.mysql import dialect
from sqlalchemy.schema import CreateColumn

from app.models import CrawlTask, Shop, ShopProfileSnapshot, TaskType
from app.storage.database import make_engine
from app.storage.schema import mysql_ddl, normalize_mysql_type, require_supported_server


PROFILE_COLUMNS = (
    "logo_url", "verification_type", "identity_label_key", "identity_label_text",
    "follower_count_text", "sales_count", "sales_count_text", "global_sales_count",
    "global_sales_count_text", "video_count", "video_count_text", "product_count_text",
    "review_count", "product_count_captured_at", "profile_captured_at", "profile_source_url",
    "profile_parser_version", "profile_response_hash", "profile_evidence_path",
    "profile_field_state", "profile_observation_status",
)
PROFILE_CHECK = "ck_shops_profile_observation_status"
TASK_CHECK = "ck_crawl_tasks_task_type"
LOCK_NAME = "tiktok_shop_profile_schema_migration"


def check_definition(table, name):
    return next(item for item in table.constraints if isinstance(item, CheckConstraint) and item.name == name)


def add_check(table, name):
    constraint = check_definition(table, name)
    expression = str(constraint.sqltext.compile(dialect=dialect(), compile_kwargs={"literal_binds": True}))
    return f"ADD CONSTRAINT `{name}` CHECK ({expression})"


def snapshot_ddl():
    value = mysql_ddl(table_names=(ShopProfileSnapshot.__tablename__,))
    return value[value.index("CREATE TABLE IF NOT EXISTS "):]


def additive_sql():
    additions = ["ADD COLUMN " + str(CreateColumn(Shop.__table__.c[name]).compile(dialect=dialect()))
                 for name in PROFILE_COLUMNS]
    additions.append(add_check(Shop.__table__, PROFILE_CHECK))
    task_column = str(CreateColumn(CrawlTask.__table__.c.task_type).compile(dialect=dialect()))
    return ("-- Shop profile extension; apply once to the existing database.\n"
            "-- For repeatable execution use python -m scripts.migrate_shop_profiles --execute.\n"
            "SET NAMES utf8mb4;\nSET time_zone = '+00:00';\n\n"
            + "ALTER TABLE shops\n    " + ",\n    ".join(additions) + ";\n\n"
            + f"ALTER TABLE crawl_tasks DROP CHECK `{TASK_CHECK}`, "
            + add_check(CrawlTask.__table__, TASK_CHECK) + ", MODIFY COLUMN " + task_column + ";\n\n"
            + snapshot_ddl())


def column_differences(actual, expected, mysql):
    differences = []
    for column in expected:
        reflected = actual.get(column.name)
        if reflected is None:
            differences.append(f"{column.table.name}.{column.name}: missing")
        elif (normalize_mysql_type(column.type.compile(dialect=mysql)) !=
              normalize_mysql_type(reflected["type"].compile(dialect=mysql))
              or column.nullable != reflected["nullable"]):
            differences.append(f"{column.table.name}.{column.name}: incompatible definition")
    return differences


def migration_plan(connection):
    """Preflight all target objects before emitting any DDL; never remove data."""
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    if not {"shops", "crawl_tasks"}.issubset(existing):
        raise ValueError("需要先初始化现有店铺与任务基础表。")
    actions = []
    actual = {item["name"]: item for item in inspector.get_columns("shops")}
    differences = column_differences(actual, [c for c in Shop.__table__.c if c.name not in PROFILE_COLUMNS], connection.dialect)
    differences += column_differences(actual, [Shop.__table__.c[name] for name in PROFILE_COLUMNS if name in actual], connection.dialect)
    task_actual = {item["name"]: item for item in inspector.get_columns("crawl_tasks")}
    differences += column_differences(task_actual, [CrawlTask.__table__.c.task_type], connection.dialect)
    if differences:
        raise ValueError("迁移目标定义不一致：" + "; ".join(differences))
    additions = ["ADD COLUMN " + str(CreateColumn(Shop.__table__.c[name]).compile(dialect=connection.dialect))
                 for name in PROFILE_COLUMNS if name not in actual]
    checks = {item["name"]: item["sqltext"] for item in inspector.get_check_constraints("shops")}
    if PROFILE_CHECK not in checks:
        additions.append(add_check(Shop.__table__, PROFILE_CHECK))
    elif set(re.findall(r"'([^']+)'", checks[PROFILE_CHECK])) != {"complete", "partial"}:
        raise ValueError("店铺档案完整度约束与预期不符。")
    if additions:
        actions.append("ALTER TABLE shops\n    " + ",\n    ".join(additions))
    checks = {item["name"]: item["sqltext"] for item in inspector.get_check_constraints("crawl_tasks")}
    if TASK_CHECK not in checks:
        raise ValueError("任务类型约束缺失，请先核查现有任务表。")
    values = set(re.findall(r"'([^']+)'", checks[TASK_CHECK]))
    expected = {kind.value for kind in TaskType}
    # Accept the historical foundation and independently installed extensions.
    # A later review task must not make the earlier profile upgrade unusable.
    foundation = expected - {TaskType.SHOP_PROFILE.value, TaskType.PRODUCT_REVIEWS.value}
    if not foundation <= values <= expected:
        raise ValueError("现有任务类型约束包含未知版本，拒绝覆盖。")
    task_actions = []
    if values != expected:
        task_actions.extend([f"DROP CHECK `{TASK_CHECK}`", add_check(CrawlTask.__table__, TASK_CHECK)])
    if task_actual["task_type"].get("comment") != CrawlTask.__table__.c.task_type.comment:
        task_actions.append("MODIFY COLUMN " + str(CreateColumn(CrawlTask.__table__.c.task_type).compile(dialect=connection.dialect)))
    if task_actions:
        actions.append("ALTER TABLE crawl_tasks " + ", ".join(task_actions))
    table = ShopProfileSnapshot.__table__
    if table.name not in existing:
        actions.extend(statement.strip() for statement in snapshot_ddl().split(";") if statement.strip())
    else:
        actual_snapshot = {item["name"]: item for item in inspector.get_columns(table.name)}
        differences = column_differences(actual_snapshot, table.c, connection.dialect)
        if set(actual_snapshot) != set(table.c.keys()):
            differences.append("shop_profile_snapshots: column names differ")
        uniques = {tuple(item["column_names"]) for item in inspector.get_unique_constraints(table.name)}
        if not {("task_id",), ("platform", "region", "shop_id", "scheduled_for")}.issubset(uniques):
            differences.append("shop_profile_snapshots: unique constraints differ")
        foreign_keys = {(tuple(item["constrained_columns"]), item["referred_table"], tuple(item["referred_columns"]),
                         item.get("options", {}).get("ondelete", "RESTRICT").upper())
                        for item in inspector.get_foreign_keys(table.name)}
        required_foreign_keys = {
            (("shop_id", "platform", "region"), "shops", ("id", "platform", "region"), "RESTRICT"),
            (("task_id",), "crawl_tasks", ("id",), "RESTRICT"),
        }
        if not required_foreign_keys.issubset(foreign_keys):
            differences.append("shop_profile_snapshots: foreign keys differ")
        indexes = {tuple(item["column_names"]) for item in inspector.get_indexes(table.name)}
        if ("shop_id", "captured_at", "id") not in indexes:
            differences.append("shop_profile_snapshots: history index missing")
        snapshot_checks = {item["name"]: item["sqltext"] for item in inspector.get_check_constraints(table.name)}
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint) and constraint.name not in snapshot_checks:
                differences.append(f"shop_profile_snapshots: {constraint.name} missing")
        status_check = snapshot_checks.get("ck_shop_profile_snapshots_observation_status", "")
        if set(re.findall(r"'([^']+)'", status_check)) != {"complete", "partial"}:
            differences.append("shop_profile_snapshots: observation status constraint differs")
        if differences:
            raise ValueError("已有档案快照表需要单独处理：" + "; ".join(differences))
    return actions


def migrate(engine, *, execute=False):
    require_supported_server(engine)
    with engine.connect() as connection:
        if not execute:
            plan = migration_plan(connection)
            return {"mode": "check", "ready": not plan, "pending_statements": len(plan), "data_rows_modified": 0}
        if connection.scalar(text("SELECT GET_LOCK(:name, 0)"), {"name": LOCK_NAME}) != 1:
            raise ValueError("已有店铺档案迁移正在执行。")
        try:
            plan = migration_plan(connection)
            for statement in plan:
                connection.exec_driver_sql(statement)
            remaining = migration_plan(connection)
            return {"mode": "execute", "ready": not remaining, "executed_statements": len(plan),
                    "pending_statements": len(remaining), "data_rows_modified": 0}
        finally:
            connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": LOCK_NAME})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--execute", action="store_true")
    modes.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute and not args.check:
        print(json.dumps({"mode": "dry_run", "database_connections": 0, "data_rows_modified": 0}))
        print(additive_sql(), end="")
        return 0
    engine = make_engine()
    try:
        result = migrate(engine, execute=args.execute)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ready"] else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "店铺档案迁移未完成；请核查目标表定义，不输出连接信息。"}, ensure_ascii=False))
        raise SystemExit(1)
