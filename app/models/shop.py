from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Index, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import (
    Base, EXTERNAL_ID, ID_TYPE, MarketMixin, PrimaryKeyMixin, TABLE_OPTIONS,
    TimestampMixin, UTCDateTime, enum_check, enum_type,
)
from .enums import ShopStatus

if TYPE_CHECKING:
    from .product import Product
    from .seed import ShopSeedLink


class Shop(PrimaryKeyMixin, MarketMixin, TimestampMixin, Base):
    __tablename__ = "shops"
    __table_args__ = (
        UniqueConstraint("platform", "region", "platform_shop_id", name="uq_shops_identity"),
        UniqueConstraint("id", "platform", "region", name="uq_shops_scope"),
        Index("ix_shops_market_status", "region", "status", "discovery_category"),
        CheckConstraint("rating IS NULL OR (rating >= 0 AND rating <= 5)", name="rating"),
        enum_check("status", ShopStatus),
        {**TABLE_OPTIONS, "comment": "已确认平台身份的正式店铺与当前指标"},
    )

    platform_shop_id: Mapped[str] = mapped_column(EXTERNAL_ID, nullable=False, comment="平台稳定店铺ID，按字符串保留前导零和大小写")
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False, comment="当前店铺名称；允许改名和不同店铺同名")
    canonical_url: Mapped[str | None] = mapped_column(Text, comment="标准化的店铺公开入口链接")
    discovery_category: Mapped[str | None] = mapped_column(String(64), comment="初次发现来源类目，不代表全部主营类目")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="USD", comment="店铺价格币种，美国站默认 USD")
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), comment="公开店铺评分，范围 0-5；不可见时为空")
    follower_count: Mapped[int | None] = mapped_column(ID_TYPE, comment="公开店铺关注数；未获取时为空")
    product_count: Mapped[int | None] = mapped_column(ID_TYPE, comment="平台展示的店铺商品数，区别于本系统已采集数量")
    status: Mapped[ShopStatus] = mapped_column(
        enum_type(ShopStatus), nullable=False, default=ShopStatus.UNKNOWN, server_default="unknown",
        comment="店铺状态：unknown=未知，active=有效，inactive=失效"
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"),
        comment="首次发现店铺的时间（UTC）"
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"),
        comment="最近成功观察到店铺的时间（UTC，业务更新）"
    )

    seed_links: Mapped[list["ShopSeedLink"]] = relationship(
        back_populates="shop", foreign_keys="ShopSeedLink.shop_id", passive_deletes="all"
    )
    products: Mapped[list["Product"]] = relationship(
        back_populates="shop",
        foreign_keys="[Product.shop_id, Product.platform, Product.region]",
        passive_deletes="all",
    )
