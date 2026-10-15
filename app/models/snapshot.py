from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import (
    Base, HASH_TYPE, ID_TYPE, CreatedAtMixin, PriceMixin, PrimaryKeyMixin,
    TABLE_OPTIONS, UTCDateTime, enum_check, enum_type, price_checks,
)
from .enums import AvailabilityStatus, StockStatus

if TYPE_CHECKING:
    from .product import Product, ProductVariant
    from .task import CrawlTask


class ProductSnapshot(PrimaryKeyMixin, PriceMixin, CreatedAtMixin, Base):
    __tablename__ = "product_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "product_id", "scheduled_for", "pricing_context", name="uq_product_snapshots_window"
        ),
        UniqueConstraint("id", "product_id", name="uq_product_snapshots_scope"),
        Index("ix_product_snapshots_history", "product_id", "captured_at"),
        Index("ix_product_snapshots_captured", "captured_at"),
        *price_checks(),
        CheckConstraint("rating IS NULL OR (rating >= 0 AND rating <= 5)", name="rating"),
        CheckConstraint(
            "source_status IS NULL OR (source_status >= 100 AND source_status <= 599)",
            name="source_status",
        ),
        CheckConstraint("variant_count IS NULL OR variant_count >= 0", name="variant_count"),
        enum_check("stock_status", StockStatus),
        enum_check("availability_status", AvailabilityStatus),
        {**TABLE_OPTIONS, "comment": "按逻辑采集窗口保存的商品价格与指标历史快照"},
    )

    product_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False,
        comment="快照所属商品内部主键，关联 products.id"
    )
    task_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("crawl_tasks.id", ondelete="RESTRICT"),
        comment="采集任务内部主键，关联 crawl_tasks.id；手动采集可空"
    )
    # Keep scheduled_for unchanged when retrying the same logical capture.
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="逻辑采集窗口时间（UTC）；同一任务重试保持不变")
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="本次实际观察时间（UTC）")
    stock_status: Mapped[StockStatus] = mapped_column(
        enum_type(StockStatus), nullable=False, default=StockStatus.UNKNOWN, server_default="unknown",
        comment="本次库存：unknown=未知，in_stock=有货，out_of_stock=缺货，partial=部分规格有货"
    )
    availability_status: Mapped[AvailabilityStatus] = mapped_column(
        enum_type(AvailabilityStatus), nullable=False,
        default=AvailabilityStatus.UNKNOWN, server_default="unknown",
        comment="本次可用状态：unknown=未知，available=可访问，unavailable=暂不可用，removed=确认下架"
    )
    sales_count: Mapped[int | None] = mapped_column(ID_TYPE, comment="本次可确认的精确公开销量；未获取时为空")
    sales_count_text: Mapped[str | None] = mapped_column(String(128), comment="本次公开销量的原始展示文案")
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), comment="本次公开商品评分，范围 0-5；未获取时为空")
    review_count: Mapped[int | None] = mapped_column(ID_TYPE, comment="本次公开评价数量；未获取时为空")
    variant_count: Mapped[int | None] = mapped_column(Integer, comment="本次成功解析的SKU规格数量")
    content_hash: Mapped[str | None] = mapped_column(HASH_TYPE, comment="本次稳定商品内容的SHA-256摘要")
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False, comment="生成该快照的解析器版本")
    source_status: Mapped[int | None] = mapped_column(Integer, comment="来源响应HTTP状态码；业务有效性另行校验")
    source_url: Mapped[str | None] = mapped_column(Text, comment="本次快照数据的公开来源链接")
    raw_payload: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(
        JSON(none_as_null=True), deferred=True,
        comment="脱敏后的来源JSON，便于复现与解析排查；默认延迟加载"
    )

    product: Mapped["Product"] = relationship(back_populates="snapshots")
    task: Mapped["CrawlTask | None"] = relationship(back_populates="snapshots")
    variant_snapshots: Mapped[list["VariantSnapshot"]] = relationship(
        back_populates="product_snapshot",
        foreign_keys="VariantSnapshot.product_snapshot_id", passive_deletes="all"
    )


class VariantSnapshot(PrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "variant_snapshots"
    __table_args__ = (
        UniqueConstraint("product_snapshot_id", "variant_id", name="uq_variant_snapshots_capture"),
        ForeignKeyConstraint(
            ["product_snapshot_id", "product_id"],
            ["product_snapshots.id", "product_snapshots.product_id"],
            name="fk_variant_snapshots_capture", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["variant_id", "product_id"],
            ["product_variants.id", "product_variants.product_id"],
            name="fk_variant_snapshots_variant", ondelete="RESTRICT",
        ),
        Index("ix_variant_snapshots_history", "variant_id", "captured_at"),
        CheckConstraint("price IS NULL OR price >= 0", name="price"),
        CheckConstraint("original_price IS NULL OR original_price >= 0", name="original_price"),
        enum_check("stock_status", StockStatus),
        {**TABLE_OPTIONS, "comment": "同一次商品采集下各SKU的价格与库存历史快照"},
    )

    product_snapshot_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="本次商品快照内部主键，关联 product_snapshots.id")
    product_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="所属商品内部主键，用于校验SKU与商品快照归属一致")
    variant_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="SKU内部主键，关联 product_variants.id")
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="该SKU本次实际观察时间（UTC）")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="USD", comment="该次SKU价格币种，美国站默认 USD")
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), comment="该SKU本次明确单价；未获取时为空")
    original_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), comment="该SKU本次展示原价；未展示时为空")
    stock_status: Mapped[StockStatus] = mapped_column(
        enum_type(StockStatus), nullable=False, default=StockStatus.UNKNOWN, server_default="unknown",
        comment="本次SKU库存：unknown=未知，in_stock=有货，out_of_stock=缺货，partial=部分可用"
    )
    available_quantity: Mapped[int | None] = mapped_column(ID_TYPE, comment="该SKU本次公开可售数量；未公开时为空")
    variant_payload_hash: Mapped[str | None] = mapped_column(HASH_TYPE, comment="该SKU本次稳定内容的SHA-256摘要")

    product_snapshot: Mapped["ProductSnapshot"] = relationship(
        back_populates="variant_snapshots", foreign_keys=[product_snapshot_id]
    )
    variant: Mapped["ProductVariant"] = relationship(
        back_populates="snapshots", foreign_keys=[variant_id]
    )
