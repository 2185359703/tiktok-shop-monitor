from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import (
    Base, HASH_TYPE, ID_TYPE, UUID_TYPE, CreatedAtMixin, MarketMixin,
    PrimaryKeyMixin, TABLE_OPTIONS, TimestampMixin, UTCDateTime, enum_check, enum_type,
)
from .enums import ResourceType, TaskStatus, TaskType

if TYPE_CHECKING:
    from .snapshot import ProductSnapshot


class CrawlTask(PrimaryKeyMixin, MarketMixin, TimestampMixin, Base):
    __tablename__ = "crawl_tasks"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_crawl_tasks_task_id"),
        UniqueConstraint("idempotency_key", name="uq_crawl_tasks_idempotency"),
        Index("ix_crawl_tasks_dispatch", "status", "available_at", "priority"),
        Index("ix_crawl_tasks_resource", "platform", "region", "resource_type", "resource_id"),
        Index("ix_crawl_tasks_lease", "status", "lease_expires_at"),
        CheckConstraint("priority >= 0 AND priority <= 9", name="priority"),
        CheckConstraint("attempt_count >= 0", name="attempt_count"),
        CheckConstraint("max_attempts >= 1", name="max_attempts"),
        enum_check("task_type", TaskType),
        enum_check("resource_type", ResourceType),
        enum_check("status", TaskStatus),
        {**TABLE_OPTIONS, "comment": "采集任务调度状态、重试、检查点与Worker租约"},
    )

    task_id: Mapped[str] = mapped_column(UUID_TYPE, nullable=False, default=lambda: str(uuid4()), comment="任务业务UUID，与数据库内部主键id不同")
    idempotency_key: Mapped[str | None] = mapped_column(HASH_TYPE, comment="任务类型、资源、窗口与上下文的SHA-256幂等键")
    task_type: Mapped[TaskType] = mapped_column(
        enum_type(TaskType), nullable=False,
        comment="任务类型：shop_discovery=店铺发现，product_discovery=商品发现，product_detail=详情，product_snapshot=快照，change_detection=变化检测，report_generation=报表"
    )
    resource_type: Mapped[ResourceType] = mapped_column(
        enum_type(ResourceType), nullable=False,
        comment="资源类型：seed=种子，shop=店铺，product=商品，variant=SKU，report=报表"
    )
    resource_id: Mapped[str] = mapped_column(String(128, collation="utf8mb4_bin"), nullable=False, comment="任务资源标识；同一资源类型使用统一的ID体系")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict, comment="任务业务参数JSON；不得保存密码或会话凭据")
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), comment="持久化分页游标或执行进度，用于断点恢复")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0", comment="调度优先级，0-9；值越大优先级越高")
    status: Mapped[TaskStatus] = mapped_column(
        enum_type(TaskStatus), nullable=False, default=TaskStatus.PENDING, server_default="pending",
        comment="任务状态：pending=待执行，running=执行中，retry_waiting=待重试，succeeded=成功，abandoned=租约失效，dead=失败耗尽，cancelled=取消"
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0", comment="已开始的执行尝试次数，含首次执行")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3", comment="最大总尝试次数，含首次执行；默认3")
    scheduled_for: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"),
        comment="原始逻辑调度窗口时间（UTC），重试时保留"
    )
    available_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"),
        comment="允许开始或再次执行的最早时间（UTC）"
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="最近一次执行开始时间（UTC）")
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="任务最终结束时间（UTC）")
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="当前Worker租约到期时间（UTC）")
    lease_token: Mapped[str | None] = mapped_column(UUID_TYPE, comment="本次执行租约UUID，防止过期Worker覆盖新执行结果")
    worker_id: Mapped[str | None] = mapped_column(String(128), comment="最近执行该任务的Worker实例标识")
    last_error: Mapped[str | None] = mapped_column(Text, comment="最近一次执行错误的脱敏描述")

    attempts: Mapped[list["CrawlAttempt"]] = relationship(
        back_populates="task", passive_deletes="all"
    )
    snapshots: Mapped[list["ProductSnapshot"]] = relationship(
        back_populates="task", passive_deletes="all"
    )


class CrawlAttempt(PrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "crawl_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_no", name="uq_crawl_attempts_number"),
        Index("ix_crawl_attempts_finished", "finished_at", "error_type"),
        CheckConstraint("attempt_no >= 1", name="attempt_no"),
        CheckConstraint(
            "response_status IS NULL OR (response_status >= 100 AND response_status <= 599)",
            name="response_status",
        ),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency_ms"),
        {**TABLE_OPTIONS, "comment": "采集任务每次执行尝试的请求结果与失败证据"},
    )

    task_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("crawl_tasks.id", ondelete="RESTRICT"), nullable=False,
        comment="任务内部主键，关联 crawl_tasks.id；不是任务业务UUID"
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="该任务的尝试序号，从1开始")
    worker_id: Mapped[str | None] = mapped_column(String(128), comment="执行该次尝试的Worker实例标识")
    request_url: Mapped[str | None] = mapped_column(Text, comment="脱敏后的请求URL，去除凭据与敏感签名")
    request_method: Mapped[str] = mapped_column(
        String(16), nullable=False, default="GET", server_default="GET",
        comment="HTTP请求方法，默认 GET"
    )
    response_status: Mapped[int | None] = mapped_column(Integer, comment="HTTP响应状态码，100-599；无响应时为空")
    response_bytes: Mapped[int | None] = mapped_column(ID_TYPE, comment="响应正文大小，单位字节")
    latency_ms: Mapped[int | None] = mapped_column(Integer, comment="请求或执行耗时，单位毫秒")
    succeeded: Mapped[bool | None] = mapped_column(Boolean, comment="该次业务处理是否成功：1=成功，0=失败，空=未完成")
    error_type: Mapped[str | None] = mapped_column(String(64), comment="可分类错误类型，例如超时、限流、解析失败")
    error_message: Mapped[str | None] = mapped_column(Text, comment="该次执行错误的脱敏描述")
    evidence_path: Mapped[str | None] = mapped_column(Text, comment="脱敏响应或排查证据文件路径")
    response_hash: Mapped[str | None] = mapped_column(HASH_TYPE, comment="响应正文的SHA-256摘要")
    started_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"),
        comment="本次执行尝试开始时间（UTC）"
    )
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="本次执行尝试结束时间（UTC）")

    task: Mapped["CrawlTask"] = relationship(back_populates="attempts")
