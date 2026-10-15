from threading import Event
from uuid import uuid4

from app.models import TaskType
from app.queue.contracts import TaskMessage
from app.settings import Settings
from scripts import run_worker


def test_shutdown_finishes_current_delivery_and_stops_intake_without_cancelling_it(monkeypatch):
    shutdown, events = Event(), []
    message = TaskMessage(str(uuid4()), TaskType.SHOP_DISCOVERY)
    config = Settings(enabled_task_types=(TaskType.SHOP_DISCOVERY, TaskType.PRODUCT_DETAIL))
    class Engine:
        def dispose(self): events.append("engine_closed")
    class Connection:
        is_open = True
    class Broker:
        connection = Connection()
        def get(self, kind):
            events.append("get:" + kind.value)
            return 7, message.to_bytes()
        def ack(self, tag): events.append("ack")
        def reject(self, tag): raise AssertionError("Current successful work must ACK")
        def close(self): events.append("broker_closed")
        def poll(self, *args): pass
    class Gate:
        def close(self): events.append("gate_closed")
    class Server:
        def shutdown(self): events.append("metrics_stopped")
        def server_close(self): pass
    class Worker:
        def __init__(self, settings, factory, registry, gate, metrics, worker_id, stop):
            self.stop = stop
        def process(self, delivered, poll):
            shutdown.set()
            assert not self.stop.is_set()
            events.append("committed")
            return "ack"
    monkeypatch.setattr("sys.argv", ["run_worker"])
    monkeypatch.setattr(run_worker, "runtime_parts", lambda: (config, {}, Engine(), None, None))
    monkeypatch.setattr(run_worker, "stop_signal", lambda: shutdown)
    monkeypatch.setattr(run_worker, "infrastructure", lambda settings: (Broker(), Gate()))
    monkeypatch.setattr(run_worker, "start_metrics", lambda *args: (Server(), None))
    monkeypatch.setattr(run_worker, "Worker", Worker)
    assert run_worker.main() == 0
    assert events[:3] == ["get:shop_discovery", "committed", "ack"]
    assert "get:product_detail" not in events
    assert "broker_closed" in events and "gate_closed" in events
