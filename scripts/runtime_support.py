"""Common runtime construction; all third-party connections remain lazy."""

import logging
import signal
from threading import Event

from prometheus_client import start_http_server

from app.observability.logging import configure_logging
from app.observability.metrics import Metrics
from app.queue.rabbitmq import RabbitMQ
from app.queue.redis_gate import RedisGate
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory
from app.workers.handlers import handler_registry, validate_enabled


def runtime_parts():
    configure_logging()
    settings = Settings.load()
    registry = handler_registry()
    validate_enabled(settings, registry)
    engine = make_engine(settings.database_url)
    return settings, registry, engine, make_session_factory(engine), Metrics()


def stop_signal():
    stop = Event()
    def handle(signum, frame):
        stop.set()
    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)
    return stop


def infrastructure(settings):
    settings.require_infrastructure()
    return RabbitMQ(settings), RedisGate(settings)


def start_metrics(metrics, port, host="127.0.0.1"):
    return start_http_server(port, addr=host, registry=metrics.registry)


def log_failure(event, error):
    # Exception strings can contain URLs, passwords or SQL parameter values.
    logging.getLogger(__name__).error(event, extra={"error_type": type(error).__name__})
