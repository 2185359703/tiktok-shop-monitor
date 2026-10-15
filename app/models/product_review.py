"""Public review facts, separate from prices and merchant content."""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, EXTERNAL_ID, HASH_TYPE, ID_TYPE, PrimaryKeyMixin, TABLE_OPTIONS, UTCDateTime


class ProductReview(PrimaryKeyMixin, Base):
    __tablename__ = "product_reviews"
    __table_args__ = (
        UniqueConstraint("product_id", "platform_review_id", name="uq_product_reviews_identity"),
        Index("ix_product_reviews_product_page", "product_id", "id"),
        CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 5", name="rating"),
        CheckConstraint("content_version >= 1", name="content_version"),
        {**TABLE_OPTIONS, "comment": "商品公开评论正文、评分与来源；不包含账户标识或会话凭据"},
    )
    product_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, comment="商品内部主键，市场与店铺沿商品关联")
    platform_review_id: Mapped[str] = mapped_column(EXTERNAL_ID, nullable=False, comment="来源评论ID，与商品共同去重")
    platform_variant_id: Mapped[str | None] = mapped_column(EXTERNAL_ID, comment="来源SKU标识；缺失保留空值")
    rating: Mapped[int | None] = mapped_column(Integer, comment="评论评分，1至5；缺失为空")
    text: Mapped[str | None] = mapped_column(Text, comment="评论纯文本正文；无正文评分允许为空")
    reviewer_name: Mapped[str | None] = mapped_column(String(255), comment="公开展示昵称；不采集用户ID或个人主页")
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="来源评论时间（UTC），未提供时为空")
    sku_specification: Mapped[str | None] = mapped_column(Text, comment="评论显示的购买规格原文")
    country: Mapped[str | None] = mapped_column(String(16), comment="评论来源国家；不可由店铺市场推断")
    verified_purchase: Mapped[bool | None] = mapped_column(Boolean, comment="来源已验证购买标记；未提供为空")
    incentivized: Mapped[bool | None] = mapped_column(Boolean, comment="来源激励评论标记；未提供为空")
    images: Mapped[list] = mapped_column(JSON, nullable=False, comment="公开买家图片链接列表；不下载媒体")
    content_hash: Mapped[str] = mapped_column(HASH_TYPE, nullable=False, comment="白名单评论字段摘要，用于识别内容更新")
    content_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="评论内容版本；内容变化时增加")
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="首次观察时间（UTC）")
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="最近成功观察时间（UTC）")
    source_url: Mapped[str] = mapped_column(Text, nullable=False, comment="去除跟踪参数的公开商品来源链接")
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False, comment="评论来源：document首屏或api分页")
    source_version: Mapped[str] = mapped_column(String(64), nullable=False, comment="评论解析器版本")
    response_hash: Mapped[str] = mapped_column(HASH_TYPE, nullable=False, comment="来源响应摘要；不保存原始响应")
    task_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("crawl_tasks.id", ondelete="RESTRICT"), nullable=False, comment="最近成功更新评论的任务内部主键")
