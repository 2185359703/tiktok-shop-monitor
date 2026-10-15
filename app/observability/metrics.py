from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class Metrics:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.completed = Counter("crawl_completed", "Completed task attempts", ["task_type", "outcome"], registry=self.registry)
        self.latency = Histogram("crawl_task_duration_seconds", "End-to-end task time", ["task_type"], registry=self.registry)
        self.active = Gauge("worker_active", "Tasks currently owned by this worker", registry=self.registry)
        self.published = Counter("crawl_notifications_published", "Confirmed RabbitMQ notifications", ["task_type"], registry=self.registry)
        self.recovered = Counter("crawl_leases_recovered", "Expired task leases recovered", registry=self.registry)
        self.queue_depth = Gauge("crawl_db_tasks", "Durable MySQL task counts", ["task_type", "status"], registry=self.registry)
        self.snapshots = Counter("snapshots_written", "Immutable product snapshots committed by this worker", registry=self.registry)
        self.variant_snapshots = Counter("variant_snapshots_written", "Immutable SKU snapshots committed by this worker", registry=self.registry)
        self.changes = Counter("change_events_written", "New comparison events committed by this worker", registry=self.registry)
