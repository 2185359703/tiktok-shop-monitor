"""Durable collection controls, explicit plans and their root-task membership."""

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, HASH_TYPE, ID_TYPE, CreatedAtMixin, MarketMixin, PrimaryKeyMixin, TABLE_OPTIONS, TimestampMixin, UTCDateTime, enum_type
from .enums import TaskType


COLLECTION_TABLE_NAMES = (
    "collection_controls", "collection_plans", "collection_runs", "collection_run_tasks", "collection_operations",
)


class CollectionControl(PrimaryKeyMixin, MarketMixin, TimestampMixin, Base):
    __tablename__ = "collection_controls"
    __table_args__ = (
        UniqueConstraint("platform", "region", name="uq_collection_controls_market"),
        {**TABLE_OPTIONS, "comment": "持久化的人工采集暂停开关；独立于来源访问保护冷却"},
    )

    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0", comment="暂停领取新的来源任务；已有租约允许完成")


class CollectionPlan(PrimaryKeyMixin, MarketMixin, TimestampMixin, Base):
    __tablename__ = "collection_plans"
    __table_args__ = (
        UniqueConstraint("request_key", name="uq_collection_plans_request"),
        Index("ix_collection_plans_due", "platform", "region", "enabled", "next_run_at", "id"),
        CheckConstraint("task_type IN ('shop_profile', 'product_snapshot')", name="task_type"),
        CheckConstraint("schedule_mode IN ('once', 'interval')", name="schedule_mode"),
        CheckConstraint("(schedule_mode = 'once' AND interval_seconds IS NULL) OR (schedule_mode = 'interval' AND interval_seconds BETWEEN 60 AND 604800)", name="interval"),
        {**TABLE_OPTIONS, "comment": "显式资源名单的单次或周期采集计划；不隐式扩大采集范围"},
    )

    request_key: Mapped[str] = mapped_column(HASH_TYPE, nullable=False, comment="创建请求的市场隔离幂等键")
    request_hash: Mapped[str] = mapped_column(HASH_TYPE, nullable=False, comment="初次计划定义摘要，拒绝复用请求ID改变定义")
    name: Mapped[str] = mapped_column(String(80), nullable=False, comment="计划显示名称")
    task_type: Mapped[TaskType] = mapped_column(enum_type(TaskType), nullable=False, comment="shop_profile或product_snapshot")
    resource_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, comment="最多100个同市场数据库资源ID字符串")
    schedule_mode: Mapped[str] = mapped_column(String(16), nullable=False, comment="once=单次，interval=周期")
    interval_seconds: Mapped[int | None] = mapped_column(Integer, comment="周期秒数；单次计划为空")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1", comment="是否允许调度器自动生成本计划的新执行")
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="下一逻辑执行时间；遗漏周期合并为一次")
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="最近生成执行记录的时间")


class CollectionRun(PrimaryKeyMixin, MarketMixin, CreatedAtMixin, Base):
    __tablename__ = "collection_runs"
    __table_args__ = (
        UniqueConstraint("run_key", name="uq_collection_runs_key"),
        Index("ix_collection_runs_plan", "plan_id", "id"),
        CheckConstraint("trigger_kind IN ('scheduled', 'manual')", name="trigger_kind"),
        CheckConstraint("requested_count BETWEEN 1 AND 100", name="requested_count"),
        {**TABLE_OPTIONS, "comment": "计划每次执行的固定逻辑窗口；状态从关联根任务推导"},
    )

    plan_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("collection_plans.id", ondelete="RESTRICT"), nullable=False, comment="所属采集计划")
    run_key: Mapped[str] = mapped_column(HASH_TYPE, nullable=False, comment="计划加逻辑窗口或手动请求ID的幂等键")
    trigger_kind: Mapped[str] = mapped_column(String(16), nullable=False, comment="scheduled或manual")
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="本次任务共同的逻辑观察窗口")
    requested_count: Mapped[int] = mapped_column(Integer, nullable=False, comment="本次固定范围内的根资源数")
    error_code: Mapped[str | None] = mapped_column(String(64), comment="生成任务失败的固定错误分类；不保存原始异常或请求")


class CollectionRunTask(PrimaryKeyMixin, Base):
    __tablename__ = "collection_run_tasks"
    __table_args__ = (
        UniqueConstraint("run_id", "task_id", name="uq_collection_run_tasks_member"),
        Index("ix_collection_run_tasks_task", "task_id"),
        CheckConstraint("member_role IN ('root', 'retry')", name="member_role"),
        {**TABLE_OPTIONS, "comment": "计划执行与根任务的精确关联；不将下游任务冒充完整覆盖"},
    )

    run_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("collection_runs.id", ondelete="RESTRICT"), nullable=False, comment="计划执行记录")
    task_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("crawl_tasks.id", ondelete="RESTRICT"), nullable=False, comment="关联的采集根任务内部ID")
    member_role: Mapped[str] = mapped_column(String(16), nullable=False, default="root", server_default="root", comment="root=本次原始根任务，retry=保留原结果的后续重试；二者都参与防重叠检查")


class CollectionOperation(PrimaryKeyMixin, MarketMixin, CreatedAtMixin, Base):
    __tablename__ = "collection_operations"
    __table_args__ = (
        Index("ix_collection_operations_market", "platform", "region", "id"),
        {**TABLE_OPTIONS, "comment": "采集控制操作审计；仅固定动作码和非敏感结构化结果"},
    )

    action: Mapped[str] = mapped_column(String(32), nullable=False, comment="固定操作分类")
    target_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="market、plan或task")
    target_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="目标数据库ID、任务UUID或市场代码")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict, comment="白名单结果字段；不保存密钥、原始请求、错误文本或任意备注")
