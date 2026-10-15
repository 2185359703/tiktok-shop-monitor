"""Shared MySQL types, UTC timestamps and database constraints."""

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from sqlalchemy import CheckConstraint, DateTime, Enum as SAEnum, FetchedValue, MetaData, Numeric, String, text
from sqlalchemy.dialects.mysql import BIGINT, CHAR, DATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}
ID_TYPE = BIGINT(unsigned=True)
EXTERNAL_ID = String(128, collation="utf8mb4_bin")
HASH_TYPE = CHAR(64, charset="ascii", collation="ascii_bin")
UUID_TYPE = CHAR(36, charset="ascii", collation="ascii_bin")


class UTCDateTime(TypeDecorator[datetime]):
    """Store UTC as DATETIME(6); return timezone-aware UTC values.

    A naive datetime is interpreted as UTC. Callers should normally supply
    datetime.now(timezone.utc), never a naive local-time datetime.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "mysql":
            return dialect.type_descriptor(DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError("UTCDateTime expects a datetime")
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "pk": "pk_%(table_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
        }
    )


class PrimaryKeyMixin:
    id: Mapped[int] = mapped_column(
        ID_TYPE, primary_key=True, autoincrement=True, comment="数据库内部自增主键"
    )


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"),
        comment="记录创建时间（UTC，微秒精度）"
    )


class TimestampMixin(CreatedAtMixin):
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
        server_onupdate=FetchedValue(),
        comment="记录最后修改时间（UTC，数据库自动更新）",
    )


class MarketMixin:
    platform: Mapped[str] = mapped_column(
        String(32, collation="utf8mb4_bin"),
        nullable=False,
        default="tiktok_shop",
        server_default="tiktok_shop",
        comment="数据来源平台，默认 tiktok_shop",
    )
    region: Mapped[str] = mapped_column(
        String(2, collation="utf8mb4_bin"), nullable=False,
        comment="市场区域代码，例如 US；统一使用大写"
    )


class PriceMixin:
    """Product-level prices are ranges, so unlike SKU prices they are not singular."""

    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="USD", server_default="USD",
        comment="价格币种代码，美国站默认 USD"
    )
    pricing_context: Mapped[str] = mapped_column(
        String(64), nullable=False, default="public_listed", server_default="public_listed",
        comment="价格观察上下文，public_listed 表示公开标价"
    )
    price_text: Mapped[str | None] = mapped_column(String(255), comment="页面原始价格文案，保留范围与展示格式")
    price_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), comment="商品当前价格下限；未获取时为空")
    price_max: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), comment="商品当前价格上限；单价商品与下限相同")
    original_price_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), comment="页面展示的原价下限；未展示时为空")
    original_price_max: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), comment="页面展示的原价上限；未展示时为空")
    discount_rate: Mapped[Decimal | None] = mapped_column(Numeric(7, 4), comment="已确认的优惠比例，范围 0-1；0.2 表示优惠两成")


def enum_type(enum_class: type[Enum], length: int = 32) -> SAEnum:
    """Persist enum values as VARCHAR; explicit CHECK constraints enforce them."""
    return SAEnum(
        enum_class,
        native_enum=False,
        create_constraint=False,
        values_callable=lambda values: [member.value for member in values],
        validate_strings=True,
        length=length,
    )


def enum_check(column: str, enum_class: type[Enum]) -> CheckConstraint:
    values = ", ".join(repr(member.value) for member in enum_class)
    return CheckConstraint(f"{column} IN ({values})", name=column)


def price_checks() -> tuple[CheckConstraint, ...]:
    return (
        CheckConstraint("price_min IS NULL OR price_min >= 0", name="price_min"),
        CheckConstraint("price_max IS NULL OR price_max >= 0", name="price_max"),
        CheckConstraint("price_min <= price_max", name="price_range"),
        CheckConstraint(
            "original_price_min IS NULL OR original_price_min >= 0", name="original_min"
        ),
        CheckConstraint(
            "original_price_max IS NULL OR original_price_max >= 0", name="original_max"
        ),
        CheckConstraint(
            "original_price_min <= original_price_max", name="original_range"
        ),
        CheckConstraint(
            "discount_rate IS NULL OR (discount_rate >= 0 AND discount_rate <= 1)",
            name="discount_rate",
        ),
    )
