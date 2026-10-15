"""MySQL tests only use a new, explicitly named disposable database."""

import os
import re

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.storage.database import make_engine
from app.storage.schema import mysql_ddl


@pytest.fixture(scope="session")
def mysql_engine():
    value = os.environ.get("MYSQL_TEST_URL")
    if not value:
        pytest.skip("Set MYSQL_TEST_URL to an isolated MySQL test database.")
    url = make_url(value)
    name = url.database or ""
    if url.host not in {"127.0.0.1", "localhost"} or not re.fullmatch(
        r"tiktok_shop_monitor_test_[A-Za-z0-9_]+", name
    ):
        raise ValueError("Tests require a local database named tiktok_shop_monitor_test_*.")
    admin = make_engine(url, with_database=False)
    engine = None
    created = False
    try:
        with admin.connect() as connection:
            if connection.scalar(
                text("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = :name"),
                {"name": name},
            ):
                raise ValueError("Test database already exists; choose a new unique name.")
        # Run the same DDL that produces migrations/001_initial_schema.sql.
        with admin.connect() as connection:
            for statement in mysql_ddl(name).split(";"):
                statement = "\n".join(
                    line for line in statement.splitlines() if not line.lstrip().startswith("--")
                ).strip()
                if statement:
                    connection.exec_driver_sql(statement)
                    if statement.startswith("CREATE DATABASE"):
                        created = True
        engine = make_engine(url)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.connect() as connection:
                connection.exec_driver_sql(f"DROP DATABASE {name}")
        admin.dispose()


@pytest.fixture
def session(mysql_engine):
    # An outer transaction rolls back all test data. Nested transactions allow
    # assertions about rejected writes without losing earlier fixtures.
    with mysql_engine.connect() as connection:
        transaction = connection.begin()
        with Session(
            connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        ) as db:
            try:
                yield db
            finally:
                db.close()
                transaction.rollback()
