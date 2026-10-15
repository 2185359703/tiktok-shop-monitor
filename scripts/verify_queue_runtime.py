"""Exercise a real disposable broker + MySQL + Redis without touching business data."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from app.crawlers.product_detail import DetailSnapshot
from app.models import Base, CrawlTask, Product, ResourceType, Shop, TaskStatus, TaskType
from app.observability.logging import configure_logging
from app.observability.metrics import Metrics
from app.queue.contracts import TaskMessage
from app.queue.rabbitmq import RabbitMQ
from app.queue.redis_gate import RedisGate
from app.services.product_detail import persist_product_detail
from app.services.report_service import ensure_report_task
from app.services.scheduler import Scheduler
from app.services.snapshot_service import persist_snapshot
from app.services.task_runtime import claim_task, database_now
from app.settings import Settings
from app.storage.database import PROJECT_ROOT, make_engine, make_session_factory, session_scope
from app.workers.handlers import handler_registry
from app.workers.runtime import Worker


def checked_metadata(path):
    path = path.resolve()
    if not path.is_relative_to((PROJECT_ROOT / "data/samples/broker-check").resolve()):
        raise ValueError("Broker 元数据需要位于项目的隔离验证目录。")
    metadata = json.loads(path.read_text(encoding="utf-8"))
    name = metadata["container"]
    if not re.fullmatch(r"tiktok-shop-broker-check-[a-f0-9]{12}", name): raise ValueError("容器名称不符合隔离作用域。")
    container_id = subprocess.run(["docker", "inspect", "--format", "{{.Id}}", name], check=True, capture_output=True, text=True).stdout.strip()
    label = subprocess.run(["docker", "inspect", "--format", '{{index .Config.Labels "codex.project"}}', name], check=True, capture_output=True, text=True).stdout.strip()
    if container_id != metadata["container_id"] or label != "tiktok-shop-monitor-broker-check":
        raise ValueError("容器身份或验证标签不一致。")
    return metadata


def settings_for(metadata, test_url):
    config = Path(metadata["config_file"]).resolve()
    if not config.is_relative_to(Path(metadata["directory"]).resolve()): raise ValueError("配置超出隔离目录。")
    broker_url = dotenv_values(config)["RABBITMQ_TEST_URL"]
    return replace(Settings.load(), database_url=test_url, rabbitmq_url=broker_url, redis_url="redis://127.0.0.1:36387/0",
                   enabled_task_types=(TaskType.REPORT_GENERATION,), periodic_snapshots_enabled=False, monitored_product_ids=(),
                   queue_prefix="verify_" + Path(metadata["directory"]).name, exchange="verify.tasks", dead_letter_exchange="verify.dead",
                   dead_letter_queue="verify.dead_letter", lease_seconds=3, heartbeat_seconds=1, retry_base_seconds=1,
                   report_directory=Path(metadata["directory"]) / "reports")


def connect_ready(broker, timeout=40):
    deadline = time.monotonic() + timeout
    while True:
        try:
            broker.connect()
            return
        except Exception:
            try: broker.close()
            except Exception: pass
            if time.monotonic() >= deadline: raise RuntimeError("隔离 Broker 没有在验证时间内就绪。") from None
            time.sleep(0.5)


def get_delivery(broker, queue, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        method, properties, body = broker.channel.basic_get(queue=queue, auto_ack=False)
        if method: return method, body
        broker.poll(0.1)
    raise RuntimeError("在等待时间内未收到预期队列消息。")


def consume(broker, worker):
    method, body = get_delivery(broker, broker.queue_name(TaskType.REPORT_GENERATION))
    message = TaskMessage.from_bytes(body)
    action = worker.process(message, broker.poll)
    (broker.ack if action == "ack" else broker.reject)(method.delivery_tag)
    broker.poll(0.1)
    return method.redelivered, message.task_id, action


def hold_unacked(metadata_path, test_url, ready_file):
    metadata = checked_metadata(metadata_path)
    settings = settings_for(metadata, test_url)
    engine, broker = make_engine(test_url), RabbitMQ(settings)
    try:
        connect_ready(broker)
        method, body = get_delivery(broker, broker.queue_name(TaskType.REPORT_GENERATION))
        message = TaskMessage.from_bytes(body)
        with session_scope(make_session_factory(engine)) as db:
            lease = claim_task(db, message.task_id, message.task_type, "crash-probe-child", settings)
        if lease is None: raise ValueError("故障探针未领取到租约。")
        ready_file.write_text(json.dumps({"task_id": message.task_id, "pid": os.getpid()}) + "\n", encoding="utf-8")
        # Parent terminates only this child after confirming its durable lease.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline: broker.poll(0.1)
    finally:
        broker.close()
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="真实 RabbitMQ 故障与重复投递联调，仅使用隔离环境")
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--detail-evidence", type=Path)
    parser.add_argument("--hold-unacked", type=Path)
    parser.add_argument("--test-url")
    args = parser.parse_args()
    configure_logging()
    if args.hold_unacked:
        url = make_url(args.test_url)
        if url.host != "127.0.0.1" or url.port != 33487 or not re.fullmatch(r"tiktok_shop_monitor_test_queue_[a-f0-9]+", url.database or ""):
            raise ValueError("子进程只能连接本次隔离测试库。")
        hold_unacked(args.metadata, args.test_url, args.hold_unacked)
        return 0
    metadata = checked_metadata(args.metadata)
    identifier = uuid4().hex[:12]
    name = f"tiktok_shop_monitor_test_queue_{identifier}"
    test_url = f"mysql+pymysql://root@127.0.0.1:33487/{name}?charset=utf8mb4"
    settings = settings_for(metadata, test_url)
    admin, engine = make_engine(test_url, with_database=False), make_engine(test_url)
    factory = make_session_factory(engine)
    broker, gate, child = RabbitMQ(settings), RedisGate(settings), None
    created, report = False, {"started_at": datetime.now(timezone.utc).isoformat(), "broker": "rabbitmq:4.1-management",
                              "uses_business_database": False, "uses_live_website": False, "checks": {}}
    try:
        with admin.connect() as db:
            if db.scalar(text("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=:name"), {"name": name}):
                raise ValueError("隔离测试库已经存在。")
            db.exec_driver_sql(f"CREATE DATABASE {name} CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
            created = True
        Base.metadata.create_all(engine)
        if args.detail_evidence is not None:
            evidence = args.detail_evidence.resolve()
            if not evidence.is_relative_to((PROJECT_ROOT / "data/samples/runtime").resolve()):
                raise ValueError("商品样例必须来自项目的成功公开投影。")
            response = DetailSnapshot.from_evidence(json.loads(evidence.read_text(encoding="utf-8")))
            with session_scope(factory) as db:
                detail = response.detail
                shop = Shop(region="US", platform_shop_id=detail.platform_shop_id, canonical_name="Observed fixture shop",
                            first_seen_at=response.captured_at, last_seen_at=response.captured_at)
                db.add(shop); db.flush()
                product = Product(region="US", shop_id=shop.id, platform_product_id=detail.platform_product_id,
                                  product_url=detail.product_url, first_seen_at=response.captured_at, last_seen_at=response.captured_at)
                db.add(product); db.flush()
                task = CrawlTask(region="US", task_type=TaskType.PRODUCT_DETAIL, resource_type=ResourceType.PRODUCT,
                                 resource_id=str(product.id), status=TaskStatus.SUCCEEDED, scheduled_for=response.started_at,
                                 payload={"platform_product_id": detail.platform_product_id, "platform_shop_id": detail.platform_shop_id})
                db.add(task); db.flush()
                persist_product_detail(db, task, response)
                persist_snapshot(db, task, response, update_current=False)
            report["fixture"] = {"source": "previous_successful_public_pdp_projection", "products": 1, "skus": len(detail.variants)}
        connect_ready(broker)
        worker = Worker(settings, factory, handler_registry(), gate, Metrics(), "broker-runtime-check")
        sequence = 0
        def new_task():
            nonlocal sequence
            sequence += 1
            with session_scope(factory) as db:
                task, _ = ensure_report_task(db, settings, database_now(db) - timedelta(microseconds=sequence))
                return task.task_id
        # Scheduler -> confirmed persistent notification -> real Worker -> commit -> ACK.
        task_id = new_task()
        summary = Scheduler(settings, factory, broker, gate, Metrics()).dispatch_once()
        assert summary["published"] == 1
        consume(broker, worker)
        with factory() as db:
            task = db.scalar(select(CrawlTask).where(CrawlTask.task_id == task_id))
            assert task.status == TaskStatus.SUCCEEDED and task.attempt_count == 1
            data = json.loads(Path(task.checkpoint["files"]["json"]).read_text(encoding="utf-8"))
            if args.detail_evidence: assert data["summary"]["products"] == 1
        report["checks"]["scheduler_worker_report_ack"] = True
        # Two notifications must still produce exactly one successful attempt.
        duplicate = new_task()
        for _ in range(2): broker.publish(TaskMessage(duplicate, TaskType.REPORT_GENERATION))
        consume(broker, worker); consume(broker, worker)
        with factory() as db:
            assert db.scalar(select(CrawlTask).where(CrawlTask.task_id == duplicate)).attempt_count == 1
        report["checks"]["duplicate_delivery_single_execution"] = True
        # Close an actual AMQP connection before ACK; broker redelivers.
        redelivery = new_task()
        broker.publish(TaskMessage(redelivery, TaskType.REPORT_GENERATION))
        get_delivery(broker, broker.queue_name(TaskType.REPORT_GENERATION))
        broker.close(); connect_ready(broker)
        redelivered, delivered_id, _ = consume(broker, worker)
        assert redelivered and delivered_id == redelivery
        report["checks"]["connection_drop_redelivery"] = True
        # Persisted queue message survives a real restart of this isolated container.
        durable = new_task()
        broker.publish(TaskMessage(durable, TaskType.REPORT_GENERATION))
        broker.close()
        checked_metadata(args.metadata)
        subprocess.run(["docker", "restart", metadata["container_id"]], check=True, capture_output=True)
        connect_ready(broker)
        _, delivered_id, _ = consume(broker, worker)
        assert delivered_id == durable
        report["checks"]["broker_restart_persistent_message"] = True
        # A physically terminated worker loses its unacked message and SQL lease.
        crash = new_task()
        broker.publish(TaskMessage(crash, TaskType.REPORT_GENERATION))
        ready_file = Path(metadata["directory"]) / "child-ready.json"
        if ready_file.exists(): ready_file.unlink()
        child = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "scripts.verify_queue_runtime", "--metadata", str(args.metadata),
                                  "--test-url", test_url, "--hold-unacked", str(ready_file)], cwd=PROJECT_ROOT,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + 20
        while not ready_file.is_file():
            if child.poll() is not None or time.monotonic() > deadline: raise RuntimeError("故障子进程没有成功领取任务。")
            broker.poll(0.1)
        assert json.loads(ready_file.read_text())["task_id"] == crash
        child.terminate(); child.wait(timeout=10); child = None
        method, body = get_delivery(broker, broker.queue_name(TaskType.REPORT_GENERATION))
        assert method.redelivered and TaskMessage.from_bytes(body).task_id == crash
        broker.ack(method.delivery_tag)
        # Real elapsed lease expiry, no manual edits to force a fabricated result.
        scheduler = Scheduler(settings, factory, broker, gate, Metrics())
        deadline = time.monotonic() + 15
        recovered = 0
        while time.monotonic() < deadline:
            state = scheduler.dispatch_once()
            recovered += state["recovered"]
            if state["published"]: break
            broker.poll(0.2)
        assert recovered == 1
        consume(broker, worker)
        with factory() as db:
            task = db.scalar(select(CrawlTask).where(CrawlTask.task_id == crash))
            assert task.status == TaskStatus.SUCCEEDED and task.attempt_count == 2
        report["checks"]["killed_worker_lease_recovery"] = True
        # Permanent invalid business payload is recorded dead before MQ dead-lettering.
        invalid = new_task()
        with session_scope(factory) as db:
            task = db.scalar(select(CrawlTask).where(CrawlTask.task_id == invalid).with_for_update())
            task.payload = {"report_type": "unsupported_report"}
        broker.publish(TaskMessage(invalid, TaskType.REPORT_GENERATION))
        _, _, action = consume(broker, worker)
        assert action == "reject"
        method, body = get_delivery(broker, settings.dead_letter_queue)
        assert TaskMessage.from_bytes(body).task_id == invalid
        broker.ack(method.delivery_tag)
        with factory() as db:
            assert db.scalar(select(CrawlTask).where(CrawlTask.task_id == invalid)).status == TaskStatus.DEAD
        report["checks"]["terminal_failure_dead_letter"] = True
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        output = Path(metadata["directory"]) / "verification.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"checks_passed": len(report["checks"]), "report": str(output)}, ensure_ascii=False))
        return 0
    finally:
        if child is not None and child.poll() is None: child.terminate(); child.wait(timeout=10)
        try: broker.close()
        except Exception: pass
        keys = list(gate.client.scan_iter(f"{gate.prefix}:*"))
        if keys: gate.client.delete(*keys)
        gate.close(); engine.dispose()
        if created:
            with admin.connect() as db: db.exec_driver_sql(f"DROP DATABASE {name}")
        admin.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "隔离队列联调未完成。"}, ensure_ascii=False))
        raise SystemExit(1)
