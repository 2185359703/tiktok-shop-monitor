"""Explicitly reviewed product and SKU relationships, independent of catalog IDs."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, JSON, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, ID_TYPE, MarketMixin, PrimaryKeyMixin, TABLE_OPTIONS, TimestampMixin, UTCDateTime, enum_check, enum_type


class MatchStatus(StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class ProductMatch(PrimaryKeyMixin, MarketMixin, TimestampMixin, Base):
    __tablename__ = "product_matches"
    __table_args__ = (
        UniqueConstraint("left_product_id", "right_product_id", name="uq_product_matches_pair"),
        UniqueConstraint("id", "left_product_id", "right_product_id", name="uq_product_matches_scope"),
        CheckConstraint("left_product_id < right_product_id", name="ordered_pair"),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence"),
        enum_check("status", MatchStatus),
        Index("ix_product_matches_market_status", "platform", "region", "status"),
        {**TABLE_OPTIONS, "comment": "人工确认或撤销的商品同款关系；有序商品对避免重复"},
    )

    left_product_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, comment="较小的商品内部主键")
    right_product_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, comment="较大的商品内部主键")
    status: Mapped[MatchStatus] = mapped_column(enum_type(MatchStatus), nullable=False, comment="关系状态：confirmed=人工确认，rejected=已撤销或排除")
    source: Mapped[str] = mapped_column(String(32), nullable=False, comment="关系来源；manual_confirmation 表示用户明确确认")
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), comment="规则建议分数，范围0至1；不是同款概率，也不是人工结论")
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False, comment="生成建议证据的规则版本")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, comment="确认时的公开标题、品牌、类目和规格证据")
    note: Mapped[str | None] = mapped_column(String(1000), comment="用户填写的匹配复核备注")
    reviewed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="最后人工确认或撤销的时间（UTC）")


class SkuMatch(PrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sku_matches"
    __table_args__ = (
        ForeignKeyConstraint(["product_match_id", "left_product_id", "right_product_id"], ["product_matches.id", "product_matches.left_product_id", "product_matches.right_product_id"], name="fk_sku_matches_product_pair", ondelete="RESTRICT"),
        ForeignKeyConstraint(["left_variant_id", "left_product_id"], ["product_variants.id", "product_variants.product_id"], name="fk_sku_matches_left_variant", ondelete="RESTRICT"),
        ForeignKeyConstraint(["right_variant_id", "right_product_id"], ["product_variants.id", "product_variants.product_id"], name="fk_sku_matches_right_variant", ondelete="RESTRICT"),
        UniqueConstraint("product_match_id", "left_variant_id", "right_variant_id", name="uq_sku_matches_pair"),
        CheckConstraint("left_product_id < right_product_id", name="ordered_product_pair"),
        CheckConstraint("left_variant_id <> right_variant_id", name="distinct_variants"),
        enum_check("status", MatchStatus),
        Index("ix_sku_matches_status", "product_match_id", "status"),
        {**TABLE_OPTIONS, "comment": "已人工复核的精确SKU映射；商品和规格归属由复合外键约束"},
    )

    product_match_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="已持久化同款商品关系的内部主键")
    left_product_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="关系左侧商品内部主键，用于校验规格归属")
    right_product_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="关系右侧商品内部主键，用于校验规格归属")
    left_variant_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="左側商品的规格内部主键")
    right_variant_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="右侧商品的规格内部主键")
    status: Mapped[MatchStatus] = mapped_column(enum_type(MatchStatus), nullable=False, comment="SKU映射状态：confirmed=人工确认，rejected=已撤销")
    source: Mapped[str] = mapped_column(String(32), nullable=False, comment="映射来源，manual_confirmation 表示用户明确确认")
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False, comment="完整规格校验规则版本")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, comment="确认时双方规格属性和价格口径证据")
    note: Mapped[str | None] = mapped_column(String(1000), comment="用户填写的SKU映射备注")
    reviewed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="最后确认或撤销时间（UTC）")
