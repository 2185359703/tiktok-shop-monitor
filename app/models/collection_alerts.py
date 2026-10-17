"""Persistent console-only collection incidents and their transition history."""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, ID_TYPE, MarketMixin, PrimaryKeyMixin, TABLE_OPTIONS, UTCDateTime


ALERT_TABLE_NAMES = ("collection_alerts", "collection_alert_events", "collection_alert_evaluations")


class CollectionAlert(PrimaryKeyMixin, MarketMixin, Base):
    __tablename__ = "collection_alerts"
    __table_args__ = (
        UniqueConstraint("platform", "region", "code", name="uq_collection_alert_market_code"),
        Index("ix_collection_alert_status", "platform", "region", "status", "id"),
        CheckConstraint("status IN ('open','acknowledged','resolved')", name="status"),
        CheckConstraint("severity IN ('info','warning','critical')", name="severity"),
        {**TABLE_OPTIONS, "comment": "同市场固定规则唯一的持久告警现态；确认不等于故障恢复"},
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False, comment="固定告警规则代码")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, comment="info、warning或critical")
    message: Mapped[str] = mapped_column(String(255), nullable=False, comment="固定规则生成的安全描述，不存请求和原始异常")
    status: Mapped[str] = mapped_column(String(16), nullable=False, comment="open未确认，acknowledged已确认，resolved已恢复")
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="首次观测到条件成立时间UTC")
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="本轮告警开始时间UTC")
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="最近观测到条件成立时间UTC")
    last_evaluated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="该规则最近可判断时间UTC；来源未知时不推进")
    condition_known: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="最新后台评估能否判断该规则；未知不作恢复")
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="本轮操作员确认时间UTC；重开清空")
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="本轮条件恢复时间UTC；重开清空")
    occurrences: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="累计告警轮次数；持续成立不累加")


class CollectionAlertEvent(PrimaryKeyMixin, Base):
    __tablename__ = "collection_alert_events"
    __table_args__ = (
        Index("ix_collection_alert_events_history", "alert_id", "id"),
        CheckConstraint("event_type IN ('opened','acknowledged','resolved','reopened')", name="event_type"),
        {**TABLE_OPTIONS, "comment": "只记录告警状态迁移的历史；重复评估不产生重复事件"},
    )
    alert_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("collection_alerts.id", ondelete="RESTRICT"), nullable=False, comment="所属持久告警主键")
    event_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="opened、acknowledged、resolved或reopened")
    status: Mapped[str] = mapped_column(String(16), nullable=False, comment="事件后的告警状态")
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="状态迁移时间UTC")
    occurrence: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="所属告警轮次")
    message: Mapped[str] = mapped_column(String(255), nullable=False, comment="当时安全描述；不记录凭据或个人信息")


class CollectionAlertEvaluation(Base):
    __tablename__ = "collection_alert_evaluations"
    __table_args__ = ({**TABLE_OPTIONS, "comment": "市场级评估串行锁与新鲜度；独立后台运行，不依赖页面访问"},)
    platform: Mapped[str] = mapped_column(String(32, collation="utf8mb4_bin"), primary_key=True, comment="来源平台")
    region: Mapped[str] = mapped_column(String(2, collation="utf8mb4_bin"), primary_key=True, comment="市场")
    last_observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="最近接受的监控观测时间UTC；防迟到评估覆盖")
    last_evaluated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="最近成功持久评估时间UTC")
    sources: Mapped[dict] = mapped_column(JSON, nullable=False, comment="各来源可用性，避免未知被解释为恢复")
