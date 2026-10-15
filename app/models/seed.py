from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Date, ForeignKeyConstraint, Index, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import (
    Base, HASH_TYPE, ID_TYPE, MarketMixin, PrimaryKeyMixin, TABLE_OPTIONS,
    TimestampMixin, UTCDateTime, enum_check, enum_type,
)
from .enums import ResolutionStatus, SeedType

if TYPE_CHECKING:
    from .shop import Shop


class SeedShopObservation(PrimaryKeyMixin, MarketMixin, TimestampMixin, Base):
    __tablename__ = "seed_shop_observations"
    __table_args__ = (
        UniqueConstraint("row_hash", name="uq_seed_observations_row_hash"),
        UniqueConstraint("id", "platform", "region", name="uq_seed_observations_scope"),
        Index("ix_seed_resolution", "region", "resolution_status", "category"),
        enum_check("seed_type", SeedType),
        enum_check("resolution_status", ResolutionStatus),
        {**TABLE_OPTIONS, "comment": "店铺种子原始观察与身份解析状态"},
    )

    category: Mapped[str] = mapped_column(String(64), nullable=False, comment="观察到店铺的服装类目代码")
    seed_type: Mapped[SeedType] = mapped_column(
        enum_type(SeedType), nullable=False, default=SeedType.SHOP_NAME, server_default="shop_name",
        comment="种子类型：shop_name=店名，shop_url=店铺链接，shop_id=平台店铺ID"
    )
    seed_value: Mapped[str] = mapped_column(String(255), nullable=False, comment="原始种子值；店名不作为正式店铺唯一身份")
    shop_url: Mapped[str | None] = mapped_column(Text, comment="原始观察中的店铺链接；尚未解析时为空")
    source_url: Mapped[str] = mapped_column(Text, nullable=False, comment="种子来源的公开类目页或发现入口")
    verification_status: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="observed_public_category_page",
        comment="原始观察的验证依据，例如已在公开类目页观察到"
    )
    observed_at: Mapped[date] = mapped_column(Date, nullable=False, comment="原始观察日期")
    row_hash: Mapped[str] = mapped_column(HASH_TYPE, nullable=False, comment="原始种子行的SHA-256摘要，用于幂等导入")
    raw_record: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), deferred=True, comment="原始CSV行的JSON内容，默认延迟加载")
    resolution_status: Mapped[ResolutionStatus] = mapped_column(
        enum_type(ResolutionStatus), nullable=False,
        default=ResolutionStatus.PENDING, server_default="pending",
        comment="解析状态：pending=待解析，resolved=已确认，ambiguous=多候选，not_found=未找到，failed=失败"
    )
    resolution_error: Mapped[str | None] = mapped_column(Text, comment="最近一次店铺身份解析失败原因")

    shop_link: Mapped["ShopSeedLink | None"] = relationship(
        back_populates="seed", foreign_keys="ShopSeedLink.seed_id",
        uselist=False, passive_deletes="all"
    )


class ShopSeedLink(PrimaryKeyMixin, MarketMixin, TimestampMixin, Base):
    __tablename__ = "shop_seed_links"
    __table_args__ = (
        UniqueConstraint("seed_id", name="uq_shop_seed_links_seed"),
        ForeignKeyConstraint(
            ["seed_id", "platform", "region"],
            ["seed_shop_observations.id", "seed_shop_observations.platform", "seed_shop_observations.region"],
            name="fk_shop_seed_links_seed", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["shop_id", "platform", "region"],
            ["shops.id", "shops.platform", "shops.region"],
            name="fk_shop_seed_links_shop", ondelete="RESTRICT",
        ),
        Index("ix_shop_seed_links_shop", "shop_id", "platform", "region"),
        {**TABLE_OPTIONS, "comment": "种子观察与正式店铺的确认映射"},
    )

    seed_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="种子观察内部主键，关联 seed_shop_observations.id")
    shop_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="正式店铺内部主键，关联 shops.id")
    resolved_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="店铺身份确认时间（UTC）")
    resolution_method: Mapped[str] = mapped_column(String(64), nullable=False, comment="身份确认方式，例如 source_id 或 manual")
    evidence_url: Mapped[str | None] = mapped_column(Text, comment="用于确认店铺身份的公开证据链接")

    seed: Mapped["SeedShopObservation"] = relationship(
        back_populates="shop_link", foreign_keys=[seed_id]
    )
    shop: Mapped["Shop"] = relationship(
        back_populates="seed_links", foreign_keys=[shop_id]
    )
