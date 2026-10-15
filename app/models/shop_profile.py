"""Immutable, market-scoped observations of a public shop's profile."""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, EXTERNAL_ID, HASH_TYPE, ID_TYPE, CreatedAtMixin, MarketMixin, PrimaryKeyMixin, TABLE_OPTIONS, UTCDateTime

if TYPE_CHECKING:
    from .shop import Shop
    from .task import CrawlTask


class ShopProfileSnapshot(PrimaryKeyMixin, MarketMixin, CreatedAtMixin, Base):
    __tablename__ = "shop_profile_snapshots"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_shop_profile_snapshots_task"),
        UniqueConstraint("platform", "region", "shop_id", "scheduled_for", name="uq_shop_profile_snapshots_window"),
        ForeignKeyConstraint(
            ["shop_id", "platform", "region"], ["shops.id", "shops.platform", "shops.region"],
            name="fk_shop_profile_snapshots_shop_scope", ondelete="RESTRICT",
        ),
        Index("ix_shop_profile_snapshots_history", "shop_id", "captured_at", "id"),
        CheckConstraint("rating IS NULL OR (rating >= 0 AND rating <= 5)", name="rating"),
        CheckConstraint("observation_status IN ('complete', 'partial')", name="observation_status"),
        {**TABLE_OPTIONS, "comment": "按逻辑采集窗口保存的店铺档案与经营指标不可变观察"},
    )

    shop_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="所属店铺内部主键，和平台及市场共同校验归属")
    task_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("crawl_tasks.id", ondelete="RESTRICT"), nullable=False, comment="档案采集任务内部主键；同一任务只能写入一次")
    platform_shop_id: Mapped[str] = mapped_column(EXTERNAL_ID, nullable=False, comment="本次确认的平台稳定店铺ID，按字符串保存")
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False, comment="本次观察到的店铺名称")
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False, comment="本次观察到的标准化店铺公开入口")
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="逻辑采集窗口时间（UTC）；任务重试保持不变")
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="本次实际读取店铺档案的时间（UTC）")
    logo_url: Mapped[str | None] = mapped_column(Text, comment="本次公开店铺头像地址；未获取时为空")
    verification_type: Mapped[str | None] = mapped_column(String(32), comment="本次来源明确展示的认证类型，例如 blue_v")
    identity_label_key: Mapped[str | None] = mapped_column(String(64), comment="本次来源店铺身份标签代码，例如 official")
    identity_label_text: Mapped[str | None] = mapped_column(String(255), comment="本次来源店铺身份标签展示文案")
    follower_count: Mapped[int | None] = mapped_column(ID_TYPE, comment="本次公开店铺粉丝数精确值；未提供精确值时为空")
    follower_count_text: Mapped[str | None] = mapped_column(String(128), comment="本次公开粉丝数原始展示文案，保留缩写和加号")
    sales_count: Mapped[int | None] = mapped_column(ID_TYPE, comment="本次公开店铺累计已售精确值；不能视为指定时期销量")
    sales_count_text: Mapped[str | None] = mapped_column(String(128), comment="本次公开店铺累计已售原始展示文案")
    global_sales_count: Mapped[int | None] = mapped_column(ID_TYPE, comment="本次来源标注的全球累计已售精确值")
    global_sales_count_text: Mapped[str | None] = mapped_column(String(128), comment="本次来源标注的全球累计已售原始展示文案")
    video_count: Mapped[int | None] = mapped_column(ID_TYPE, comment="本次店铺页展示的视频数量；不推断视频归属口径")
    video_count_text: Mapped[str | None] = mapped_column(String(128), comment="本次店铺页视频数原始展示文案")
    product_count: Mapped[int | None] = mapped_column(ID_TYPE, comment="本次平台展示的在售商品数，区别于本系统已采集数量")
    product_count_text: Mapped[str | None] = mapped_column(String(128), comment="本次在售商品数原始展示文案")
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), comment="本次公开店铺评分，范围0-5；未获取时为空")
    review_count: Mapped[int | None] = mapped_column(ID_TYPE, comment="本次公开店铺评价数量；未获取时为空")
    source_url: Mapped[str] = mapped_column(Text, nullable=False, comment="本次公开来源链接，移除跟踪与签名参数")
    response_hash: Mapped[str] = mapped_column(HASH_TYPE, nullable=False, comment="本次来源响应的SHA-256摘要")
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False, comment="生成本次店铺档案的解析器版本")
    evidence_path: Mapped[str | None] = mapped_column(Text, comment="本次白名单业务证据文件路径")
    field_state: Mapped[dict] = mapped_column(JSON, nullable=False, comment="本次各字段状态、来源路径及指标口径，不包含原始页面或凭据")
    observation_status: Mapped[str] = mapped_column(String(16), nullable=False, comment="本次观察完整度：complete=完整，partial=部分字段缺失或无效")

    shop: Mapped["Shop"] = relationship(
        back_populates="profile_snapshots",
        foreign_keys="[ShopProfileSnapshot.shop_id, ShopProfileSnapshot.platform, ShopProfileSnapshot.region]",
    )
    task: Mapped["CrawlTask"] = relationship()
