"""Public photo links live separately from actively collected product rows."""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, HASH_TYPE, ID_TYPE, PrimaryKeyMixin, TABLE_OPTIONS, UTCDateTime


class ProductImage(PrimaryKeyMixin, Base):
    __tablename__ = "product_images"
    __table_args__ = (
        UniqueConstraint("product_id", "image_key", name="uq_product_images_identity"),
        Index("ix_product_images_cover", "product_id", "is_current", "position"),
        CheckConstraint("role IN ('cover', 'gallery')", name="role"),
        CheckConstraint("source_kind IN ('detail', 'listing')", name="source_kind"),
        CheckConstraint("position >= 0 AND position < 100", name="position"),
        {**TABLE_OPTIONS, "comment": "公开商品原图与缩略图链接、来源及最新相册顺序"},
    )
    product_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False,
                                          comment="商品内部主键")
    image_key: Mapped[str] = mapped_column(HASH_TYPE, nullable=False, comment="来源照片 URI 的 SHA256 业务去重键")
    image_url: Mapped[str] = mapped_column(Text, nullable=False, comment="平台公开图片 HTTP(S) 链接")
    thumbnail_url: Mapped[str | None] = mapped_column(Text, comment="平台公开缩略图链接，缺失时为空")
    source_url: Mapped[str] = mapped_column(Text, nullable=False, comment="观察图片的公开商品或店铺页面")
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False, comment="detail=详情相册，listing=店铺列表封面")
    source_version: Mapped[str] = mapped_column(String(64), nullable=False, comment="图片解析版本，独立于价格和 SKU 哈希")
    role: Mapped[str] = mapped_column(String(16), nullable=False, comment="cover=封面，gallery=相册图片")
    position: Mapped[int] = mapped_column(Integer, nullable=False, comment="公开相册顺序，从零开始")
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("1"),
                                          comment="是否在最新完整且非空的公开相册中出现")
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="图片最近观察时间（UTC）")
