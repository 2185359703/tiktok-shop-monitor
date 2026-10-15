"""MySQL engine and transactional SQLAlchemy sessions."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_project_environment() -> None:
    """Load local secrets once without overwriting the process environment."""
    for name in (".env", "tiktok-shop-monitor.env"):
        config = PROJECT_ROOT / name
        if config.is_file():
            load_dotenv(config, override=False)
            break


def configured_database_url() -> URL:
    """Environment wins; otherwise .env wins over tiktok-shop-monitor.env."""
    load_project_environment()
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise ValueError(
            "Set DATABASE_URL in .env or tiktok-shop-monitor.env in the project root."
        )
    url = safe_database_url(value)
    validate_database_url(url)
    return url


def safe_database_url(value: str | URL) -> URL:
    try:
        return make_url(value)
    except ArgumentError:
        raise ValueError("数据库连接格式不正确，请检查本地配置。") from None


def validate_database_url(url: URL) -> None:
    if url.drivername not in {"mysql+pymysql", "mysql+aiomysql"}:
        raise ValueError("DATABASE_URL must use mysql+pymysql or mysql+aiomysql.")
    if not url.database:
        raise ValueError("DATABASE_URL must name the project database.")


def make_engine(database_url: str | URL | None = None, *, with_database: bool = True) -> Engine:
    url = configured_database_url() if database_url is None else safe_database_url(database_url)
    validate_database_url(url)
    # Require consistent encoding without relying on the server's current defaults.
    url = url.set(drivername="mysql+pymysql").update_query_dict({"charset": "utf8mb4"})
    if not with_database:
        # URL.set(database=None) means "leave unchanged" in SQLAlchemy.
        # An empty database connects without selecting a schema in PyMySQL.
        url = url.set(database="")
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=int(os.environ.get("DB_POOL_RECYCLE", "1800")),
        pool_size=int(os.environ.get("DB_POOL_SIZE", "5")),
        max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "10")),
        connect_args={"connect_timeout": 10},
    )

    @event.listens_for(engine, "connect")
    def set_session_options(dbapi_connection, connection_record):
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET time_zone = '+00:00'")
            cursor.execute(
                "SET SESSION sql_mode = "
                "'STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
                "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION'"
            )

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit only on success; roll back failures; always close the session."""
    with factory.begin() as session:
        yield session
