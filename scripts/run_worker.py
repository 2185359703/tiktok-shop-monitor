import argparse
import json
import logging
import socket
from uuid import uuid4

from app.queue.contracts import TaskMessage
from app.workers.runtime import Worker
from scripts.runtime_support import infrastructure, log_failure, runtime_parts, start_metrics, stop_signal


def main():
    parser = argparse.ArgumentParser(description="租约与消息确认驱动的采集 Worker")
    parser.add_argument("--check", action="store_true", help="只显示适配器状态，不连接数据库、队列或网站")
    parser.add_argument("--once", action="store_true", help="最多处理一条消息")
    args = parser.parse_args()
    settings, registry, engine, factory, metrics = runtime_parts()
    broker = gate = server = None
    try:
        if args.check:
            print(json.dumps({"implemented_task_types": sorted(registry),
                              "enabled_task_types": list(settings.enabled_task_types)}, ensure_ascii=False))
            return 0
        stop = stop_signal()
        worker_id = f"{socket.gethostname()}:{uuid4().hex[:12]}"[:128]
        if not args.once:
            server, _ = start_metrics(metrics, settings.worker_metrics_port, settings.host)
        if not settings.enabled_task_types:
            logging.getLogger(__name__).info("worker_idle_no_collectors_enabled")
            if args.once:
                return 0
            while not stop.wait(1):
                pass
            return 0
        broker, gate = infrastructure(settings)
        worker = Worker(settings, factory, registry, gate, metrics, worker_id, stop)
        while not stop.is_set():
            try:
                if broker.connection is None or not broker.connection.is_open:
                    broker.connect()
                found = False
                for kind in settings.enabled_task_types:
                    delivery = broker.get(kind)
                    if delivery is None:
                        continue
                    found = True
                    tag, body = delivery
                    try:
                        message = TaskMessage.from_bytes(body)
                        if message.task_type != kind:
                            raise ValueError("通知路由与任务类型不一致。")
                    except ValueError:
                        broker.reject(tag)
                        continue
                    action = worker.process(message, broker.poll)
                    (broker.ack if action == "ack" else broker.reject)(tag)
                    if args.once:
                        return 0
                if args.once:
                    return 0
                if not found:
                    broker.poll(0.5)
            except Exception as error:
                log_failure("worker_retry_connection", error)
                try:
                    broker.close()
                except Exception:
                    pass
                if args.once:
                    return 1
                stop.wait(settings.poll_seconds)
        return 0
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if broker:
            try:
                broker.close()
            except Exception:
                pass
        if gate:
            gate.close()
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        log_failure("invalid_runtime_configuration", error)
        raise SystemExit(1)
