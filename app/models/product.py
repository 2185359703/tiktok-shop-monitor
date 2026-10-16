from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint, ForeignKey, ForeignKeyConstraint, Index,
    Integer, JSON, Numeric, String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import (
    Base, EXTERNAL_ID, HASH_TYPE, ID_TYPE, MarketMixin, PriceMixin,
    PrimaryKeyMixin, TABLE_OPTIONS, TimestampMixin, UTCDateTime,
    enum_check, enum_type, price_checks,
)
from .enums import AvailabilityStatus, StockStatus

if TYPE_CHECKING:
    from .shop import Shop
    from .snapshot import ProductSnapshot, VariantSnapshot


class Product(PrimaryKeyMixin, MarketMixin, PriceMixin, TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("platform", "region", "platform_product_id", name="uq_products_identity"),
        ForeignKeyConstraint(
            ["shop_id", "platform", "region"],
            ["shops.id", "shops.platform", "shops.region"],
            name="fk_products_shop_scope", ondelete="RESTRICT",
        ),
        Index("ix_products_shop_status", "shop_id", "availability_status"),
        Index("ix_products_category_price", "region", "normalized_category", "price_min"),
        Index("ix_products_next_capture", "availability_status", "next_capture_at"),
        Index("ix_products_recent_detail", "platform", "region", "last_success_at", "id", "title"),
        *price_checks(),
        CheckConstraint("data_version >= 1", name="data_version"),
        CheckConstraint("rating IS NULL OR (rating >= 0 AND rating <= 5)", name="rating"),
        enum_check("stock_status", StockStatus),
        enum_check("availability_status", AvailabilityStatus),
        {**TABLE_OPTIONS, "comment": "商品当前详情、服装属性、价格区间与采集状态"},
    )

    platform_product_id: Mapped[str] = mapped_column(EXTERNAL_ID, nullable=False, comment="平台稳定商品ID，按字符串保存")
    shop_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="所属店铺内部主键，关联 shops.id")
    product_url: Mapped[str] = mapped_column(Text, nullable=False, comment="商品公开详情入口链接")
    title: Mapped[str | None] = mapped_column(String(512), comment="商品标题；发现阶段可暂缺，详情解析应校验")
    description: Mapped[str | None] = mapped_column(Text, deferred=True, comment="商品原始描述，默认延迟加载")
    original_category: Mapped[str | None] = mapped_column(Text, comment="平台原始类目路径")
    normalized_category: Mapped[str | None] = mapped_column(String(64), comment="清洗后的服装标准类目代码")
    category_rule_version: Mapped[str | None] = mapped_column(String(32), comment="类目清洗规则版本")
    brand: Mapped[str | None] = mapped_column(String(255), comment="公开商品品牌")
    gender: Mapped[str | None] = mapped_column(String(32), comment="适用性别或人群标签")
    season: Mapped[str | None] = mapped_column(String(64), comment="适用季节标签")
    style: Mapped[str | None] = mapped_column(String(128), comment="服装风格标签，例如休闲、运动")
    fit: Mapped[str | None] = mapped_column(String(64), comment="服装版型，例如修身、宽松")
    garment_length: Mapped[str | None] = mapped_column(String(64), comment="衣长、裤长或裙长属性")
    material: Mapped[str | None] = mapped_column(Text, comment="面料与材质描述")
    pattern: Mapped[str | None] = mapped_column(String(128), comment="图案或纹理标签")
    color_summary: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True), comment="商品可见颜色标签数组")
    size_summary: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True), comment="商品可见尺码标签数组，保留原始体系")
    raw_attributes: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), deferred=True, comment="原始服装属性JSON，便于复核清洗结果")
    stock_status: Mapped[StockStatus] = mapped_column(
        enum_type(StockStatus), nullable=False, default=StockStatus.UNKNOWN, server_default="unknown",
        comment="库存状态：unknown=未知，in_stock=有货，out_of_stock=缺货，partial=部分规格有货"
    )
    availability_status: Mapped[AvailabilityStatus] = mapped_column(
        enum_type(AvailabilityStatus), nullable=False,
        default=AvailabilityStatus.UNKNOWN, server_default="unknown",
        comment="可用状态：unknown=未知，available=可访问，unavailable=暂不可用，removed=确认下架"
    )
    sales_count: Mapped[int | None] = mapped_column(ID_TYPE, comment="可确认的精确公开销量；缺失或仅有区间时为空")
    sales_count_text: Mapped[str | None] = mapped_column(String(128), comment="公开销量原始文案，例如 1K+ sold")
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), comment="公开商品评分，范围 0-5；未获取时为空")
    review_count: Mapped[int | None] = mapped_column(ID_TYPE, comment="公开评价数量；未获取时为空")
    first_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"),
        comment="首次发现商品的时间（UTC）"
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"),
        comment="最近成功观察到商品的时间（UTC，业务更新）"
    )
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="最近有效详情采集完成时间（UTC）")
    next_capture_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="下一次计划采集时间（UTC）")
    content_hash: Mapped[str | None] = mapped_column(HASH_TYPE, comment="稳定业务内容SHA-256摘要，排除采集时间与随机字段")
    parser_version: Mapped[str | None] = mapped_column(String(32), comment="当前商品详情解析器版本")
    data_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1", comment="业务数据版本，默认1；内容变更时由服务层增加")

    shop: Mapped["Shop"] = relationship(
        back_populates="products",
        foreign_keys="[Product.shop_id, Product.platform, Product.region]",
    )
    variants: Mapped[list["ProductVariant"]] = relationship(
        back_populates="product", passive_deletes="all"
    )
    snapshots: Mapped[list["ProductSnapshot"]] = relationship(
        back_populates="product", passive_deletes="all"
    )


class ProductVariant(PrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "product_variants"
    __table_args__ = (
        UniqueConstraint("product_id", "variant_key", name="uq_variants_identity"),
        UniqueConstraint("product_id", "platform_variant_id", name="uq_variants_platform_id"),
        UniqueConstraint("id", "product_id", name="uq_variants_product_scope"),
        CheckConstraint("price IS NULL OR price >= 0", name="price"),
        CheckConstraint("original_price IS NULL OR original_price >= 0", name="original_price"),
        enum_check("stock_status", StockStatus),
        {**TABLE_OPTIONS, "comment": "商品颜色、尺码等SKU规格的当前价格与库存"},
    )

    product_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False,
        comment="所属商品内部主键，关联 products.id"
    )
    variant_key: Mapped[str] = mapped_column(HASH_TYPE, nullable=False, comment="规格身份SHA-256键，由平台SKU ID或稳定规格组合生成")
    platform_variant_id: Mapped[str | None] = mapped_column(EXTERNAL_ID, comment="平台原始SKU ID；未提供时为空")
    color: Mapped[str | None] = mapped_column(String(128), comment="该SKU的颜色标签")
    size: Mapped[str | None] = mapped_column(String(64), comment="该SKU的尺码标签，保留原始体系")
    material: Mapped[str | None] = mapped_column(String(255), comment="该SKU的面料或材质")
    options: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), comment="完整规格键值组合JSON")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="USD", comment="SKU价格币种，美国站默认 USD")
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), comment="该SKU的当前明确单价；未获取时为空")
    original_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), comment="该SKU页面展示的原价；未展示时为空")
    stock_status: Mapped[StockStatus] = mapped_column(
        enum_type(StockStatus), nullable=False, default=StockStatus.UNKNOWN, server_default="unknown",
        comment="SKU库存：unknown=未知，in_stock=有货，out_of_stock=缺货，partial=部分可用"
    )
    available_quantity: Mapped[int | None] = mapped_column(ID_TYPE, comment="平台可见可售数量；未公开时为空")
    variant_payload_hash: Mapped[str | None] = mapped_column(HASH_TYPE, comment="稳定SKU内容的SHA-256摘要")

    product: Mapped["Product"] = relationship(back_populates="variants")
    snapshots: Mapped[list["VariantSnapshot"]] = relationship(
        back_populates="variant", foreign_keys="VariantSnapshot.variant_id", passive_deletes="all"
    )
