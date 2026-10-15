"""Allowlisted logs: never serialize exceptions, headers, payloads or DSNs."""

import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {"at": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
                "logger": record.name, "event": record.getMessage()}
        for name in ("task_id", "task_type", "worker_id", "attempt", "status", "error_type", "latency_ms", "count"):
            if hasattr(record, name):
                data[name] = getattr(record, name)
        if record.exc_info:
            data["exception_type"] = record.exc_info[0].__name__
        return json.dumps(data, ensure_ascii=False, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # Request/connection libraries may include credentials or signed URLs.
    for name in ("httpx", "httpcore", "pika", "sqlalchemy.engine", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
