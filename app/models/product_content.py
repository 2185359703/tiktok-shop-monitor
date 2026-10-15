"""Latest independently versioned merchant content, outside the catalog hash."""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, HASH_TYPE, ID_TYPE, PrimaryKeyMixin, TABLE_OPTIONS, UTCDateTime


class ProductContent(PrimaryKeyMixin, Base):
    __tablename__ = "product_contents"
    __table_args__ = (
        UniqueConstraint("product_id", name="uq_product_contents_product"),
        CheckConstraint("completeness IN ('missing', 'empty', 'partial', 'complete')", name="completeness"),
        CheckConstraint("content_version >= 1", name="content_version"),
        {**TABLE_OPTIONS, "comment": "商品公开介绍的安全有序节点、参数与独立采集证据"},
    )
    product_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False,
                                          comment="商品内部主键，每个商品保留一份最新介绍")
    description_text: Mapped[str | None] = mapped_column(Text().with_variant(MEDIUMTEXT(), "mysql"),
                                                       comment="商家介绍纯文本投影，不含评论与推断内容")
    sections: Mapped[dict] = mapped_column(JSON, nullable=False, comment="商品参数与介绍的有序安全节点JSON")
    completeness: Mapped[str] = mapped_column(String(16), nullable=False,
                                             comment="内容覆盖：missing缺失、empty显式空、partial部分、complete完整")
    unsupported_blocks: Mapped[list] = mapped_column(JSON, nullable=False, comment="未支持节点或缺失结构的原因代码数组")
    source_shape: Mapped[dict] = mapped_column(JSON, nullable=False, comment="白名单来源字段存在性、节点类型计数与章节覆盖")
    source_version: Mapped[str] = mapped_column(String(64), nullable=False, comment="介绍解析版本，独立于商品与图片解析版本")
    content_hash: Mapped[str] = mapped_column(HASH_TYPE, nullable=False, comment="介绍节点、文本与覆盖状态的SHA256摘要")
    content_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="独立介绍版本，仅内容摘要变化时增加")
    source_url: Mapped[str] = mapped_column(Text, nullable=False, comment="实际观察介绍的公开商品详情链接")
    response_hash: Mapped[str] = mapped_column(HASH_TYPE, nullable=False, comment="本次公开详情响应的SHA256证据摘要")
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="当前介绍最近成功观察时间（UTC）")

