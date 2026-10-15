from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, ForeignKeyConstraint, Index, JSON, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import (
    Base, HASH_TYPE, ID_TYPE, CreatedAtMixin, PrimaryKeyMixin, TABLE_OPTIONS,
    UTCDateTime, enum_check, enum_type,
)
from .enums import ChangeEventType

if TYPE_CHECKING:
    from .product import Product, ProductVariant
    from .snapshot import ProductSnapshot


class ChangeEvent(PrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "change_events"
    __table_args__ = (
        UniqueConstraint("event_hash", name="uq_change_events_hash"),
        ForeignKeyConstraint(
            ["snapshot_id", "product_id"],
            ["product_snapshots.id", "product_snapshots.product_id"],
            name="fk_change_events_snapshot", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["previous_snapshot_id", "product_id"],
            ["product_snapshots.id", "product_snapshots.product_id"],
            name="fk_change_events_previous", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["variant_id", "product_id"],
            ["product_variants.id", "product_variants.product_id"],
            name="fk_change_events_variant", ondelete="RESTRICT",
        ),
        Index("ix_change_events_product_time", "product_id", "detected_at"),
        Index("ix_change_events_type_time", "event_type", "detected_at"),
        enum_check("event_type", ChangeEventType),
        {**TABLE_OPTIONS, "comment": "商品与SKU变化事件及前后快照证据"},
    )

    product_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False,
        comment="发生变化的商品内部主键，关联 products.id"
    )
    variant_id: Mapped[int | None] = mapped_column(ID_TYPE, comment="发生变化的SKU内部主键；商品级事件可空")
    snapshot_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="变化后的商品快照主键，关联 product_snapshots.id")
    previous_snapshot_id: Mapped[int | None] = mapped_column(ID_TYPE, comment="变化前的商品快照主键；首次发现商品时为空")
    event_type: Mapped[ChangeEventType] = mapped_column(
        enum_type(ChangeEventType), nullable=False,
        comment="事件：product_new=新发现，price_changed=价格变化，discount_started/ended=折扣始末，stockout/restocked=缺补货，variant_changed/content_changed=规格或内容变化，product_removed=确认下架"
    )
    old_value: Mapped[Any | None] = mapped_column(JSON(none_as_null=True), comment="变化前的业务值JSON；金额用字符串保存")
    new_value: Mapped[Any | None] = mapped_column(JSON(none_as_null=True), comment="变化后的业务值JSON；金额用字符串保存")
    detected_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"),
        comment="检测到该变化的时间（UTC）"
    )
    event_hash: Mapped[str] = mapped_column(HASH_TYPE, nullable=False, comment="变化事件SHA-256幂等键，避免同一对快照重复生成事件")

    product: Mapped["Product"] = relationship(foreign_keys=[product_id])
    variant: Mapped["ProductVariant | None"] = relationship(foreign_keys=[variant_id])
    snapshot: Mapped["ProductSnapshot"] = relationship(foreign_keys=[snapshot_id])
    previous_snapshot: Mapped["ProductSnapshot | None"] = relationship(
        foreign_keys=[previous_snapshot_id]
    )
