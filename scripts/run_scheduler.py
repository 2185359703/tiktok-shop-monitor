import argparse
import json
import logging

from app.services.scheduler import Scheduler, task_counts
from scripts.runtime_support import infrastructure, log_failure, runtime_parts, start_metrics, stop_signal


def main():
    parser = argparse.ArgumentParser(description="持久化任务调度、通知补发和过期租约恢复")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="只读数据库，不连接 MQ/Redis、不恢复租约、不发布消息")
    args = parser.parse_args()
    settings, registry, engine, factory, metrics = runtime_parts()
    broker = gate = server = None
    try:
        if args.dry_run:
            with factory() as db:
                counts = task_counts(db, settings)
            print(json.dumps({"enabled_task_types": list(settings.enabled_task_types),
                              "counts": [{"task_type": kind, "status": status, "count": count}
                                         for kind, status, count in counts]}, ensure_ascii=False))
            return 0
        broker, gate = infrastructure(settings)
        scheduler = Scheduler(settings, factory, broker, gate, metrics)
        stop = stop_signal()
        if not args.once:
            server, _ = start_metrics(metrics, settings.scheduler_metrics_port, settings.host)
        while not stop.is_set():
            try:
                if broker.connection is None or not broker.connection.is_open:
                    broker.connect()
                summary = scheduler.dispatch_once()
                logging.getLogger(__name__).info("scheduler_tick", extra={"count": summary["published"]})
                if args.once:
                    print(json.dumps(summary))
                    return 0
                # Pump AMQP events while waiting, even for longer poll intervals.
                remaining = settings.poll_seconds
                while remaining > 0 and not stop.is_set():
                    broker.poll(min(0.5, remaining))
                    remaining -= 0.5
            except Exception as error:
                log_failure("scheduler_retry_connection", error)
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
