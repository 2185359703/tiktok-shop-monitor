"""Validated TOML settings plus protected environment-only connection secrets."""

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from app.models import TaskType
from app.storage.database import PROJECT_ROOT, load_project_environment


@dataclass(frozen=True)
class Settings:
    platform: str = "tiktok_shop"
    region: str = "US"
    currency: str = "USD"
    pricing_context: str = "public_listed"
    enabled_task_types: tuple[TaskType, ...] = ()
    lease_seconds: int = 120
    heartbeat_seconds: int = 15
    poll_seconds: float = 5
    dispatch_batch_size: int = 100
    dispatch_scan_limit: int = 1000
    dispatch_queue_high_watermark: int = 64
    dispatch_local_queue_high_watermark: int = 16
    publish_dedup_seconds: int = 30
    retry_base_seconds: int = 10
    retry_max_seconds: int = 300
    product_discovery_max_pages: int = 50
    snapshot_interval_seconds: int = 3600
    periodic_snapshots_enabled: bool = False
    baseline_on_detail: bool = False
    detail_http_backend: str = "httpx"
    resource_document_cooldown: bool = False
    monitored_product_ids: tuple[str, ...] = ()
    exchange: str = "tiktok_shop.tasks"
    queue_prefix: str = "tiktok_shop"
    broker_generation: str = ""
    dead_letter_exchange: str = "tiktok_shop.dead"
    dead_letter_queue: str = "tiktok_shop.dead_letter"
    prefetch: int = 1
    minimum_interval_seconds: float = 2.0
    access_pause_seconds: int = 900
    evidence_directory: Path = PROJECT_ROOT / "data/samples/runtime"
    report_directory: Path = PROJECT_ROOT / "data/exports/reports"
    collection_directory: Path = PROJECT_ROOT / "data/collections"
    host: str = "127.0.0.1"
    port: int = 8000
    worker_metrics_port: int = 8001
    scheduler_metrics_port: int = 8002
    database_url: str | None = field(default=None, repr=False)
    rabbitmq_url: str | None = field(default=None, repr=False)
    redis_url: str | None = field(default=None, repr=False)
    api_token: str | None = field(default=None, repr=False)
    detail_proxy_url: str | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.platform != "tiktok_shop" or self.region != "US" or self.currency != "USD":
            raise ValueError("当前适配器仅支持 tiktok_shop / US / USD。")
        positive = (self.lease_seconds, self.heartbeat_seconds, self.poll_seconds,
                    self.dispatch_batch_size, self.dispatch_scan_limit, self.dispatch_queue_high_watermark,
                    self.dispatch_local_queue_high_watermark, self.publish_dedup_seconds,
                    self.retry_base_seconds, self.retry_max_seconds, self.prefetch,
                    self.access_pause_seconds)
        if any(value <= 0 for value in positive):
            raise ValueError("调度、租约和重试配置必须为正数。")
        if self.heartbeat_seconds * 3 > self.lease_seconds:
            raise ValueError("租约至少需要覆盖三个心跳周期。")
        if self.dispatch_scan_limit < self.dispatch_batch_size or self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("扫描范围和最大退避不能小于各自的基准值。")
        if self.minimum_interval_seconds < 2:
            raise ValueError("公开网站共享请求间隔不得小于 2 秒。")
        if type(self.product_discovery_max_pages) is not int or not 1 <= self.product_discovery_max_pages <= 500:
            raise ValueError("商品发现页数预算必须是 1-500 的整数；达到预算不代表全店完成。")
        if type(self.snapshot_interval_seconds) is not int or not 60 <= self.snapshot_interval_seconds <= 86400:
            raise ValueError("快照间隔必须是 60-86400 秒的整数。")
        if type(self.periodic_snapshots_enabled) is not bool:
            raise ValueError("周期快照开关必须是布尔值。")
        if type(self.baseline_on_detail) is not bool:
            raise ValueError("详情基线开关必须是布尔值。")
        if self.detail_http_backend not in {"httpx", "curl_cffi"}:
            raise ValueError("详情 HTTP 客户端必须是 httpx 或 curl_cffi。")
        if type(self.resource_document_cooldown) is not bool:
            raise ValueError("资源验证页冷却开关必须是布尔值。")
        if (len(set(self.monitored_product_ids)) != len(self.monitored_product_ids)
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,128}", value) for value in self.monitored_product_ids)):
            raise ValueError("监测名单只能包含唯一的平台商品 ID 字符串。")
        if self.periodic_snapshots_enabled and (not self.monitored_product_ids or not {
                TaskType.PRODUCT_SNAPSHOT, TaskType.CHANGE_DETECTION}.issubset(self.enabled_task_types)):
            raise ValueError("周期监测需要显式商品名单，并同时启用快照与变化检测处理器。")
        for value in (self.exchange, self.queue_prefix, self.dead_letter_exchange, self.dead_letter_queue):
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value):
                raise ValueError("消息队列名称格式不正确。")
        if not isinstance(self.broker_generation, str) or (self.broker_generation and not re.fullmatch(
                r"[A-Za-z0-9_-]{1,32}", self.broker_generation)):
            raise ValueError("Broker 代际标识必须为空或 1-32 个字母、数字、下划线、短横线。")
        for value, schemes in ((self.rabbitmq_url, {"amqp", "amqps"}), (self.redis_url, {"redis", "rediss"}),
                               (self.detail_proxy_url, {"http", "https", "socks5", "socks5h"})):
            if value:
                try:
                    parsed = urlsplit(value)
                    if parsed.scheme not in schemes or not parsed.hostname:
                        raise ValueError
                    _ = parsed.port
                except ValueError:
                    raise ValueError("连接配置的协议、主机或端口不正确。") from None
        if self.api_token is not None and len(self.api_token) < 24:
            raise ValueError("API_TOKEN 至少需要 24 个字符；不配置时写接口关闭。")
        if any(not 1 <= port <= 65535 for port in (self.port, self.worker_metrics_port, self.scheduler_metrics_port)):
            raise ValueError("服务端口超出范围。")

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        load_project_environment()
        path = path or Path(os.environ.get("SETTINGS_FILE", PROJECT_ROOT / "configs/settings.toml"))
        with path.open("rb") as stream:
            config = tomllib.load(stream)
        values = {}
        for section in ("scope", "runtime", "queue", "request", "server", "report"):
            values.update(config.get(section, {}))
        raw_types = os.environ.get("ENABLED_TASK_TYPES")
        types = [item.strip() for item in raw_types.split(",") if item.strip()] if raw_types is not None else values.get("enabled_task_types", [])
        try:
            values["enabled_task_types"] = tuple(dict.fromkeys(TaskType(item) for item in types))
        except ValueError:
            raise ValueError("enabled_task_types 中包含未知任务类型。") from None
        directory = Path(values.get("evidence_directory", "data/samples/runtime"))
        values["evidence_directory"] = directory if directory.is_absolute() else PROJECT_ROOT / directory
        report_directory = Path(values.get("report_directory", "data/exports/reports"))
        values["report_directory"] = report_directory if report_directory.is_absolute() else PROJECT_ROOT / report_directory
        collection_directory = Path(values.get("collection_directory", "data/collections"))
        values["collection_directory"] = collection_directory if collection_directory.is_absolute() else PROJECT_ROOT / collection_directory
        values["monitored_product_ids"] = tuple(values.get("monitored_product_ids", ()))
        values["host"] = os.environ.get("API_HOST", values.get("host", "127.0.0.1"))
        values["port"] = int(os.environ.get("API_PORT", values.get("port", 8000)))
        for key in ("database_url", "rabbitmq_url", "redis_url", "api_token", "detail_proxy_url"):
            values[key] = os.environ.get(key.upper()) or None
        return cls(**values)

    def require_infrastructure(self) -> None:
        if not self.rabbitmq_url or not self.redis_url:
            raise ValueError("调度器和 Worker 需要配置 RABBITMQ_URL 与 REDIS_URL。")
