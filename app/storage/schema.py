"""Compile the initial DDL or create the configured MySQL tables."""

import argparse
import re
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import Engine, inspect, literal, text
from sqlalchemy.dialects.mysql import dialect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CreateColumn, CreateIndex, CreateTable
from sqlalchemy.sql.ddl import sort_tables

from app.models import Base
from app.storage.database import configured_database_url, make_engine


DEFAULT_DATABASE = "tiktok_shop_monitor"

# Historical migrations describe this immutable ten-table foundation. New tables
# belong to additive migrations and the current full-schema snapshot.
INITIAL_TABLE_NAMES = (
    "change_events", "crawl_attempts", "crawl_tasks", "product_snapshots", "products",
    "product_variants", "seed_shop_observations", "shop_seed_links", "shops", "variant_snapshots",
)
WORKBENCH_TABLE_NAMES = ("product_images", "product_matches", "sku_matches")


def model_tables(table_names: Iterable[str] | None = None):
    """Sort only the selected tables, preserving historical dependency order."""
    if table_names is None:
        return Base.metadata.sorted_tables
    names = set(table_names)
    unknown = names - Base.metadata.tables.keys()
    if unknown:
        raise ValueError("Unknown model tables: " + ", ".join(sorted(unknown)))
    return sort_tables([Base.metadata.tables[name] for name in sorted(names)])


def validate_database_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", name):
        raise ValueError("Database names must contain 1-64 letters, digits or underscores.")
    return name


def mysql_ddl(database_name: str = DEFAULT_DATABASE, *, table_names: Iterable[str] | None = None) -> str:
    name = validate_database_name(database_name)
    configure_mappers()
    mysql = dialect()
    statements = [
        "-- Generated from app.models by python -m app.storage.schema --print-sql",
        "-- Requires MySQL >= 8.0.16. All datetime values use UTC.",
        f"CREATE DATABASE IF NOT EXISTS {name} CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
        f"USE {name};",
        "SET time_zone = '+00:00';",
        (
            "SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
            "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION';"
        ),
    ]
    for table in model_tables(table_names):
        compiled = str(CreateTable(table, if_not_exists=True).compile(dialect=mysql)).strip()
        # SQLAlchemy's MySQL compiler emits table options using a set. Fix their
        # ordering so the saved SQL is identical across Python processes.
        body, _ = compiled.rsplit("\n)", 1)
        options = table.dialect_options["mysql"]
        statements.append(
            body + "\n) ENGINE=" + options["engine"]
            + " CHARSET=" + options["charset"]
            + " COLLATE=" + options["collate"]
            + (" COMMENT=" + comment_literal(table.comment, mysql) if table.comment else "")
            + ";"
        )
        statements.extend(
            str(CreateIndex(index).compile(dialect=mysql)).strip() + ";"
            for index in sorted(table.indexes, key=lambda item: item.name or "")
        )
    return "\n".join(line.rstrip() for line in "\n\n".join(statements).splitlines()) + "\n"


def comment_literal(value: str, mysql_dialect) -> str:
    return str(literal(value).compile(
        dialect=mysql_dialect, compile_kwargs={"literal_binds": True}
    ))


def mysql_comments_ddl(database_name: str = DEFAULT_DATABASE, *, table_names: Iterable[str] | None = None) -> str:
    """Comment migration for an existing database with the initial model schema."""
    name = validate_database_name(database_name)
    mysql = dialect()
    statements = [
        "-- Generated comment migration from app.models.",
        "-- Existing columns retain their model definitions, keys and constraints.",
        f"USE {name};",
        "SET NAMES utf8mb4;",
    ]
    for table in model_tables(table_names):
        actions = ["COMMENT=" + comment_literal(table.comment, mysql)]
        actions.extend(
            "MODIFY COLUMN " + str(CreateColumn(column).compile(dialect=mysql))
            for column in table.c
        )
        statements.append(
            "ALTER TABLE " + mysql.identifier_preparer.quote_identifier(table.name)
            + "\n    " + ",\n    ".join(actions) + ";"
        )
    return "\n\n".join(statements) + "\n"


def pending_comment_statements(engine: Engine) -> list[str]:
    """Build comment-only ALTERs, preserving the live column definitions.

    SHOW CREATE TABLE preserves attributes such as AUTO_INCREMENT, collation,
    DEFAULT and ON UPDATE, which must be repeated in MODIFY COLUMN.
    """
    differences = schema_drift(engine)
    if differences:
        raise ValueError("Existing schema needs a migration: " + "; ".join(differences))
    statements = []
    mysql = engine.dialect
    quote = mysql.identifier_preparer.quote_identifier
    with engine.connect() as connection:
        inspector = inspect(connection)
        missing = set(Base.metadata.tables) - set(inspector.get_table_names())
        if missing:
            raise ValueError("Create missing tables first: " + ", ".join(sorted(missing)))
        for table in Base.metadata.sorted_tables:
            actions = []
            if inspector.get_table_comment(table.name)["text"] != table.comment:
                actions.append("COMMENT=" + comment_literal(table.comment, mysql))
            actual = {
                column["name"]: column for column in inspector.get_columns(table.name)
            }
            create_sql = connection.exec_driver_sql(
                "SHOW CREATE TABLE " + quote(table.name)
            ).one()[1]
            definitions = {}
            for line in create_sql.splitlines():
                stripped = line.strip().removesuffix(",")
                for column in table.c:
                    prefix = quote(column.name) + " "
                    if stripped.startswith(prefix):
                        definitions[column.name] = stripped
                        break
            for column in table.c:
                if actual[column.name].get("comment") == column.comment:
                    continue
                definition = definitions[column.name]
                if actual[column.name].get("comment"):
                    clean = re.sub(
                        r"\s+COMMENT\s+'(?:''|\\.|[^'\\])*'\s*$", "", definition
                    )
                    if clean == definition:
                        raise ValueError(
                            f"Cannot safely replace comment on {table.name}.{column.name}."
                        )
                    definition = clean
                actions.append(
                    "MODIFY COLUMN " + definition
                    + " COMMENT " + comment_literal(column.comment, mysql)
                )
            if actions:
                statements.append(
                    "ALTER TABLE " + quote(table.name) + "\n    " + ",\n    ".join(actions)
                )
    return statements


def sync_comments(engine: Engine) -> int:
    """Update only mismatched comments; a repeated call emits no ALTERs."""
    statements = pending_comment_statements(engine)
    with engine.connect() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement, execution_options={"no_parameters": True})
    return len(statements)


def require_supported_server(engine: Engine) -> str:
    with engine.connect() as connection:
        version = str(connection.scalar(text("SELECT VERSION()")))
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    if "mariadb" in version.lower() or not match or tuple(map(int, match.groups())) < (8, 0, 16):
        raise ValueError("This schema requires MySQL 8.0.16 or newer.")
    return version


def schema_drift(engine: Engine) -> list[str]:
    """Reject edits to existing columns instead of silently altering stored data."""
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    differences = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue
        actual = {column["name"]: column for column in inspector.get_columns(table.name)}
        expected = set(table.c.keys())
        if set(actual) != expected:
            differences.append(f"{table.name}: column names differ")
            continue
        for column in table.c:
            reflected = actual[column.name]
            expected_type = normalize_mysql_type(column.type.compile(dialect=engine.dialect))
            actual_type = normalize_mysql_type(reflected["type"].compile(dialect=engine.dialect))
            if expected_type != actual_type or column.nullable != reflected["nullable"]:
                differences.append(f"{table.name}.{column.name}: type or nullability differs")
    return differences


def normalize_mysql_type(value: str) -> str:
    """Normalize MySQL's reflected aliases without dropping precision or collation."""
    value = value.upper().replace("NUMERIC(", "DECIMAL(")
    if value in {"BOOL", "BOOLEAN"}:
        value = "TINYINT(1)"
    # MySQL adds the inferred charset for a non-default utf8mb4 collation.
    return value.replace(" CHARACTER SET UTF8MB4", "")


def initialize_schema(*, create_database: bool = False) -> tuple[str, int]:
    configure_mappers()
    url = configured_database_url()
    name = validate_database_name(url.database or "")
    if create_database:
        admin = make_engine(url, with_database=False)
        try:
            version = require_supported_server(admin)
            with admin.begin() as connection:
                connection.execute(text(
                    f"CREATE DATABASE IF NOT EXISTS {name} "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                ))
        finally:
            admin.dispose()
    engine = make_engine(url)
    try:
        version = require_supported_server(engine)
        differences = schema_drift(engine)
        if differences:
            raise ValueError("Existing schema needs a migration: " + "; ".join(differences))
        Base.metadata.create_all(engine, checkfirst=True)
        missing = set(Base.metadata.tables) - set(inspect(engine).get_table_names())
        if missing:
            raise ValueError("Tables not created: " + ", ".join(sorted(missing)))
        return version, len(Base.metadata.tables)
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create-database", action="store_true")
    parser.add_argument("--print-sql", action="store_true", help="Print SQL without connecting.")
    parser.add_argument(
        "--print-comments-sql", action="store_true", help="Print the comment migration offline."
    )
    parser.add_argument(
        "--sync-comments", action="store_true", help="Sync Chinese table and column comments."
    )
    parser.add_argument("--database-name", default=DEFAULT_DATABASE, help="For --print-sql only.")
    parser.add_argument("--check-sql", type=Path, help="Check a saved SQL file against the models.")
    args = parser.parse_args()
    try:
        if args.check_sql:
            saved = args.check_sql.read_text(encoding="utf-8")
            if saved.replace("\r\n", "\n") != mysql_ddl(args.database_name):
                raise ValueError("SQL file does not match the current models.")
            print("SQL file matches all models.")
            return 0
        if args.print_sql:
            print(mysql_ddl(args.database_name), end="")
            return 0
        if args.print_comments_sql:
            print(mysql_comments_ddl(args.database_name), end="")
            return 0
        if args.sync_comments:
            engine = make_engine()
            try:
                require_supported_server(engine)
                count = sync_comments(engine)
                print(f"Chinese comments updated on {count} tables.")
            finally:
                engine.dispose()
            return 0
        version, count = initialize_schema(create_database=args.create_database)
        print(f"MySQL {version}: {count} tables are ready.")
        return 0
    except ValueError as exc:
        print(f"Schema initialization failed: {exc}")
    except SQLAlchemyError as exc:
        # Avoid printing a connection string or sensitive SQL parameters.
        args = getattr(getattr(exc, "orig", None), "args", ())
        code = args[0] if args else "unknown"
        print(f"Schema initialization failed: {type(exc).__name__} (MySQL code {code}).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
