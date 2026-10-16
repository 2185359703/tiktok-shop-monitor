"""Rebuildable UI projections; source observations remain authoritative."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, JSON, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, ID_TYPE, TABLE_OPTIONS, UTCDateTime


class ProductReadModel(Base):
    __tablename__ = "product_read_models"
    __table_args__ = (
        CheckConstraint("basis IN ('current','latest_valid_snapshot')", name="basis"),
        Index("ix_read_products_recent", "platform", "region", "basis", "observed_at", "product_id"),
        Index("ix_read_products_shop", "platform", "region", "basis", "shop_id", "product_id"),
        Index("ix_read_products_price", "platform", "region", "basis", "price_min", "product_id"),
        Index("ix_read_products_title", "platform", "region", "basis", "title", "product_id"),
        Index("ix_read_products_sku", "platform", "region", "basis", "sku_count", "product_id"),
        Index("ix_read_products_stock", "platform", "region", "basis", "stock_status", "product_id"),
        Index("ix_read_products_scope", "schema_version", "basis", "platform", "region", "currency", "pricing_context",
              "eligible", "shop_id", "product_id"),
        Index("ix_read_products_catalog", "schema_version", "basis", "platform", "region", "currency", "pricing_context",
              "eligible", "observed_at", "product_id", "shop_id", "price_min", "stock_status", "sku_count", "rating", "review_count", "title"),
        {**TABLE_OPTIONS, "comment": "可重建的商品分页与统计投影；区分当前观察和历史快照口径"},
    )
    schema_version: Mapped[int] = mapped_column(Integer, primary_key=True, comment="派生结构版本")
    basis: Mapped[str] = mapped_column(String(32), primary_key=True, comment="统计口径")
    product_id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=False, comment="源商品主键")
    platform: Mapped[str] = mapped_column(String(32), nullable=False, comment="来源平台")
    region: Mapped[str] = mapped_column(String(2), nullable=False, comment="市场")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, comment="币种")
    pricing_context: Mapped[str] = mapped_column(String(64), nullable=False, comment="报价上下文")
    shop_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="源店铺主键")
    platform_product_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="平台商品标识")
    title: Mapped[str | None] = mapped_column(String(512), comment="该口径商品标题")
    category: Mapped[str] = mapped_column(Text, nullable=False, comment="该口径有效类目")
    brand: Mapped[str | None] = mapped_column(String(255), comment="公开品牌")
    price_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), comment="公开起价")
    stock_status: Mapped[str] = mapped_column(String(32), nullable=False, comment="库存状态；未知不置零")
    sku_count: Mapped[int | None] = mapped_column(Integer, comment="当前观察规格数量；未采详情为空")
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), comment="公开评分")
    review_count: Mapped[int | None] = mapped_column(ID_TYPE, comment="公开评价数")
    observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="源观察时间")
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="是否满足本口径来源条件")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, comment="不含源大JSON和正文的列表字段")
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, deferred=True, comment="仅后台读取的规格计数与历史质量")
    applied_revision: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="已发布刷新修订号")
    projected_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="投影生成时间")


class EventReadModel(Base):
    __tablename__ = "event_read_models"
    __table_args__ = (
        Index("ix_read_events_time", "platform", "region", "observed_at", "event_id"),
        Index("ix_read_events_type", "platform", "region", "event_type", "observed_at", "event_id"),
        {**TABLE_OPTIONS, "comment": "从不可变快照提取的历史事件分页投影"},
    )
    schema_version: Mapped[int] = mapped_column(Integer, primary_key=True, comment="派生结构版本")
    event_id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=False, comment="源事件主键")
    platform: Mapped[str] = mapped_column(String(32), nullable=False, comment="来源平台")
    region: Mapped[str] = mapped_column(String(2), nullable=False, comment="市场")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, comment="快照币种")
    pricing_context: Mapped[str] = mapped_column(String(64), nullable=False, comment="快照报价上下文")
    product_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="商品主键")
    shop_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="店铺主键")
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="事件类型")
    title: Mapped[str | None] = mapped_column(String(512), comment="事件发生时的商品标题")
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="后快照观察时间")
    published_sequence: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="同事务发布序号；分页水位")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, comment="历史规格和前后值；保留源快照引用")


class AnalyticsRefreshRequest(Base):
    __tablename__ = "analytics_refresh_requests"
    __table_args__ = (
        Index("ix_analytics_due", "kind", "available_at", "lease_until"),
        Index("ix_analytics_pending", "schema_version", "is_pending", "available_at", "key_hash"),
        CheckConstraint("requested_revision >= completed_revision", name="revision_order"),
        {**TABLE_OPTIONS, "comment": "按资源合并的统计刷新需求及租约控制"},
    )
    schema_version: Mapped[int] = mapped_column(Integer, primary_key=True, comment="派生结构版本")
    kind: Mapped[str] = mapped_column(String(32), primary_key=True, comment="刷新种类或控制行")
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True, comment="规范化参数SHA256")
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, comment="作用域与有界参数")
    requested_revision: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="最新请求修订")
    completed_revision: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="成功处理修订")
    claimed_revision: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="本次领取修订")
    is_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="到期扫描索引标记；修订号仍为处理依据")
    pending_since: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="最早未处理请求时间")
    available_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="可再次领取时间")
    last_requested_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="最近访问或请求时间")
    lease_token: Mapped[str | None] = mapped_column(String(36), comment="领取所有权令牌")
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="数据库时间下的租约期限")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, comment="连续失败次数")
    last_error_code: Mapped[str | None] = mapped_column(String(80), comment="脱敏失败类型")


class AnalyticsSnapshot(Base):
    __tablename__ = "analytics_snapshots"
    __table_args__ = ({**TABLE_OPTIONS, "comment": "原子发布的小型图表统计；重建期间保留上一成功结果"},)
    schema_version: Mapped[int] = mapped_column(Integer, primary_key=True, comment="派生结构版本")
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True, comment="规范化查询键")
    kind: Mapped[str] = mapped_column(String(32), nullable=False, comment="统计主题")
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, comment="市场、口径与过滤条件")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, comment="有限摘要，不含商品全集")
    generation: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="发布修订")
    snapshot_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="派生读取视图时间")
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="本结果生成时间")
    fresh_until: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="新鲜期截止")
    stale_until: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="允许显示旧结果的截止时间")


class AnalyticsProductRollup(Base):
    __tablename__ = "analytics_product_rollups"
    __table_args__ = ({**TABLE_OPTIONS, "comment": "每128个商品一片的增量统计；与商品投影同事务发布"},)
    schema_version: Mapped[int] = mapped_column(Integer, primary_key=True, comment="分片统计结构版本")
    basis: Mapped[str] = mapped_column(String(32), primary_key=True, comment="当前观察或最新有效历史快照口径")
    shard_id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=False, comment="商品主键除以128的分片编号")
    members: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, deferred=True, comment="有界商品贡献；按修订替换以保证幂等")
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, comment="按市场及报价上下文隔离的精确统计摘要")


ROLLUP_TABLE_NAMES = (AnalyticsProductRollup.__tablename__,)
ANALYTICS_TABLE_NAMES = tuple(table.__tablename__ for table in (
    ProductReadModel, EventReadModel, AnalyticsRefreshRequest, AnalyticsSnapshot))
