from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Index, JSON, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import (
    Base, EXTERNAL_ID, HASH_TYPE, ID_TYPE, MarketMixin, PrimaryKeyMixin, TABLE_OPTIONS,
    TimestampMixin, UTCDateTime, enum_check, enum_type,
)
from .enums import ShopStatus

if TYPE_CHECKING:
    from .product import Product
    from .seed import ShopSeedLink
    from .shop_profile import ShopProfileSnapshot


class Shop(PrimaryKeyMixin, MarketMixin, TimestampMixin, Base):
    __tablename__ = "shops"
    __table_args__ = (
        UniqueConstraint("platform", "region", "platform_shop_id", name="uq_shops_identity"),
        UniqueConstraint("id", "platform", "region", name="uq_shops_scope"),
        Index("ix_shops_market_status", "region", "status", "discovery_category"),
        CheckConstraint("rating IS NULL OR (rating >= 0 AND rating <= 5)", name="rating"),
        CheckConstraint("profile_observation_status IS NULL OR profile_observation_status IN ('complete', 'partial')", name="profile_observation_status"),
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
    logo_url: Mapped[str | None] = mapped_column(Text, comment="本次观察到的公开店铺头像地址；未获取时为空")
    verification_type: Mapped[str | None] = mapped_column(String(32), comment="来源明确展示的认证类型，例如 blue_v；空不代表未认证")
    identity_label_key: Mapped[str | None] = mapped_column(String(64), comment="来源店铺身份标签代码，例如 official")
    identity_label_text: Mapped[str | None] = mapped_column(String(255), comment="来源店铺身份标签展示文案，例如 OFFICIAL SHOP")
    follower_count_text: Mapped[str | None] = mapped_column(String(128), comment="本次公开粉丝数原始展示文案，保留缩写和加号")
    sales_count: Mapped[int | None] = mapped_column(ID_TYPE, comment="本次公开店铺累计已售精确值；不能视为指定时期销量")
    sales_count_text: Mapped[str | None] = mapped_column(String(128), comment="本次公开店铺累计已售原始展示文案")
    global_sales_count: Mapped[int | None] = mapped_column(ID_TYPE, comment="本次来源标注的全球累计已售精确值；未获取时为空")
    global_sales_count_text: Mapped[str | None] = mapped_column(String(128), comment="本次来源标注的全球累计已售原始展示文案")
    video_count: Mapped[int | None] = mapped_column(ID_TYPE, comment="本次店铺页展示的视频数量；不推断视频归属口径")
    video_count_text: Mapped[str | None] = mapped_column(String(128), comment="本次店铺页展示的视频数原始文案")
    product_count_text: Mapped[str | None] = mapped_column(String(128), comment="平台展示的在售商品数原始文案；与商品数观察时间一致")
    review_count: Mapped[int | None] = mapped_column(ID_TYPE, comment="本次公开店铺评价数量；未获取时为空")
    product_count_captured_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="当前平台商品数的独立实际观察时间（UTC）")
    profile_captured_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="当前店铺档案实际观察时间（UTC），独立于商品发现时间")
    profile_source_url: Mapped[str | None] = mapped_column(Text, comment="当前档案的公开来源链接，移除跟踪与签名参数")
    profile_parser_version: Mapped[str | None] = mapped_column(String(64), comment="当前店铺档案解析器版本")
    profile_response_hash: Mapped[str | None] = mapped_column(HASH_TYPE, comment="当前店铺档案来源响应的SHA-256摘要")
    profile_evidence_path: Mapped[str | None] = mapped_column(Text, comment="当前档案白名单业务证据文件路径")
    profile_field_state: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), comment="当前档案各字段状态、来源路径和指标口径")
    profile_observation_status: Mapped[str | None] = mapped_column(String(16), comment="当前档案观察完整度：complete=完整，partial=部分字段缺失或无效")
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
    profile_snapshots: Mapped[list["ShopProfileSnapshot"]] = relationship(
        back_populates="shop",
        foreign_keys="[ShopProfileSnapshot.shop_id, ShopProfileSnapshot.platform, ShopProfileSnapshot.region]",
        passive_deletes="all",
    )
