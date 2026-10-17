"""Operator-created source collection batches and exact task membership."""

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, HASH_TYPE, ID_TYPE, CreatedAtMixin, MarketMixin, PrimaryKeyMixin, TABLE_OPTIONS, enum_type
from .base import UTCDateTime
from datetime import datetime
from .enums import TaskType


LAUNCH_TABLE_NAMES = ("collection_launches", "collection_launch_tasks")


class CollectionLaunch(PrimaryKeyMixin, MarketMixin, CreatedAtMixin, Base):
    __tablename__ = "collection_launches"
    __table_args__ = (
        UniqueConstraint("request_key", name="uq_collection_launches_request"),
        Index("ix_collection_launches_market", "platform", "region", "id"),
        Index("ix_collection_launches_cancellation", "platform", "region", "cancel_pending", "cancel_checked_at", "id"),
        CheckConstraint("requested_count BETWEEN 1 AND 100", name="requested_count"),
        CheckConstraint("task_type IN ('shop_discovery', 'product_discovery', 'product_detail', 'product_reviews')", name="task_type"),
        {**TABLE_OPTIONS, "comment": "人工明确范围的一次性采集批次；保存幂等结果与真实任务链路"},
    )

    request_key: Mapped[str] = mapped_column(HASH_TYPE, nullable=False, comment="市场与提交UUID生成的唯一幂等键")
    request_hash: Mapped[str] = mapped_column(HASH_TYPE, nullable=False, comment="规范化采集定义摘要；同一请求不能改变范围")
    name: Mapped[str] = mapped_column(String(80), nullable=False, comment="用户可识别的批次名称")
    task_type: Mapped[TaskType] = mapped_column(enum_type(TaskType), nullable=False, comment="本批次最初派发的来源任务类型")
    requested_count: Mapped[int] = mapped_column(Integer, nullable=False, comment="明确提交且去重后的资源或店铺链接数量")
    created_task_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0", comment="本次新建的根任务数量")
    reused_task_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0", comment="复用已存在根任务的数量；不表示新增来源请求")
    max_pages: Mapped[int | None] = mapped_column(Integer, comment="每个资源的分页上限；仅商品发现与评论适用")
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="持久取消请求时间；阻止独占后续任务开始")
    cancel_completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="当前关联的活动任务全部结束时间；保留共享任务的实际执行历史")
    cancel_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0", comment="是否仍需有界清理；共享任务活动时保留检查资格")
    cancel_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="最近清理检查时间；用于公平轮转而非始终重复最早的批次")


class CollectionLaunchTask(PrimaryKeyMixin, Base):
    __tablename__ = "collection_launch_tasks"
    __table_args__ = (
        UniqueConstraint("launch_id", "task_id", name="uq_collection_launch_tasks_member"),
        Index("ix_collection_launch_tasks_task", "task_id"),
        CheckConstraint("member_role IN ('root', 'descendant', 'retry')", name="member_role"),
        {**TABLE_OPTIONS, "comment": "一次性采集的根任务、后续分页处理及保留历史的重试成员"},
    )

    launch_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("collection_launches.id", ondelete="RESTRICT"), nullable=False, comment="所属人工采集批次内部ID")
    task_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("crawl_tasks.id", ondelete="RESTRICT"), nullable=False, comment="准确关联的任务内部ID")
    member_role: Mapped[str] = mapped_column(String(16), nullable=False, comment="root为原始根任务，descendant为后续任务，retry为补采任务")
