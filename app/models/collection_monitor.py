"""Rebuildable, market-scoped completion facts and minute statistics."""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, JSON, PrimaryKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, ID_TYPE, TABLE_OPTIONS, UTCDateTime


MONITOR_TABLE_NAMES = ("collection_attempt_facts", "collection_minute_rollups", "collection_monitor_states")


class MonitorScope:
    schema_version: Mapped[int] = mapped_column(Integer, primary_key=True, comment="可重建统计结构版本")
    platform: Mapped[str] = mapped_column(String(32, collation="utf8mb4_bin"), primary_key=True, comment="来源平台")
    region: Mapped[str] = mapped_column(String(2, collation="utf8mb4_bin"), primary_key=True, comment="市场")


class CollectionAttemptFact(Base):
    __tablename__ = "collection_attempt_facts"
    __table_args__ = (
        Index("ix_collection_facts_pending", "schema_version", "platform", "region", "processed", "finished_at", "attempt_id"),
        Index("ix_collection_facts_failures", "schema_version", "platform", "region", "succeeded", "finished_at", "attempt_id"),
        Index("ix_collection_facts_group_failures", "schema_version", "platform", "region", "task_group", "succeeded", "finished_at", "attempt_id"),
        CheckConstraint("task_group IN ('source','local')", name="task_group"),
        {**TABLE_OPTIONS, "comment": "与尝试完成同事务写入的幂等事实；仅保留监控所需安全字段"},
    )
    schema_version: Mapped[int] = mapped_column(Integer, primary_key=True, comment="派生结构版本")
    attempt_id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=False, comment="源尝试主键与幂等键；不是完成水位")
    platform: Mapped[str] = mapped_column(String(32, collation="utf8mb4_bin"), nullable=False, comment="来源平台")
    region: Mapped[str] = mapped_column(String(2, collation="utf8mb4_bin"), nullable=False, comment="市场")
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, comment="任务业务UUID")
    task_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="任务类型；来源与本地处理分别统计")
    task_group: Mapped[str] = mapped_column(String(8), nullable=False, comment="来源采集source或本地处理local；支持有界失败明细")
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="任务尝试序号")
    worker_id: Mapped[str | None] = mapped_column(String(128), comment="执行实例标识")
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="源尝试完成时间UTC")
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="业务尝试是否成功")
    error_type: Mapped[str | None] = mapped_column(String(64), comment="白名单格式错误分类；不含异常详情")
    response_status: Mapped[int | None] = mapped_column(Integer, comment="HTTP响应状态")
    duration_us: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="完整执行耗时微秒；非HTTP单次延迟")
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0", comment="是否与分钟计数同事务完成投影")


class CollectionMinuteRollup(MonitorScope, Base):
    __tablename__ = "collection_minute_rollups"
    __table_args__ = (
        PrimaryKeyConstraint("schema_version", "platform", "region", "minute_at", "task_type"),
        CheckConstraint("succeeded <= completed", name="success_count"),
        {**TABLE_OPTIONS, "comment": "每市场每任务类型分钟统计；准确计数与耗时和，直方图分位数为显式近似"},
    )
    minute_at: Mapped[datetime] = mapped_column(UTCDateTime(), primary_key=True, comment="UTC分钟起点，左闭右开")
    task_type: Mapped[str] = mapped_column(String(32), primary_key=True, comment="任务类型")
    completed: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="完成尝试总数")
    succeeded: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="成功尝试总数")
    duration_us: Mapped[int] = mapped_column(ID_TYPE, nullable=False, comment="完整执行耗时总和微秒")
    histogram: Mapped[dict] = mapped_column(JSON, nullable=False, comment="1.1倍几何耗时直方图；P95取桶上界")
    errors: Mapped[dict] = mapped_column(JSON, nullable=False, comment="安全错误分类计数")


class CollectionMonitorState(MonitorScope, Base):
    __tablename__ = "collection_monitor_states"
    __table_args__ = ({**TABLE_OPTIONS, "comment": "新写入器切换后显式激活的可恢复监控回填进度；缺失代表未激活"},)
    activated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="新写入器已启用后的激活时间UTC")
    coverage_since: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="当前回填或修复覆盖起点UTC")
    backfill_until: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="当前回填或修复固定终点UTC，不随重启改变")
    cursor_finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="按完成时间排序的历史回填游标")
    cursor_attempt_id: Mapped[int | None] = mapped_column(ID_TYPE, comment="相同完成时间的分页辅助键，不是完成水位")
    backfill_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0", comment="固定历史区间是否已完整扫描")
    generation: Mapped[int] = mapped_column(ID_TYPE, nullable=False, default=0, server_default="0", comment="完整统计快照发布代数")
    last_progress_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, comment="后台最近成功处理时间")
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), comment="最近完整快照发布时间")
