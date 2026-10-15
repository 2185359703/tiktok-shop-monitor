"""One-context, single-flight cache of immutable report response bytes."""

import time
from concurrent.futures import Future
from dataclasses import dataclass
from threading import Lock

from app.reports.exporter import html_response_bytes, json_chunks, render_markdown, response_bytes


@dataclass(frozen=True)
class SerializedReport:
    generated_at: str
    bodies: dict[str, bytes]


def serialize_report(report):
    json_body = response_bytes(json_chunks(report))
    return SerializedReport(report["generated_at"], {
        "json": json_body,
        "html": html_response_bytes(json_body),
        "md": render_markdown(report).encode("utf-8"),
    })


class ReportCache:
    def __init__(self, ttl_seconds=60, clock=time.monotonic):
        self.ttl_seconds, self.clock = ttl_seconds, clock
        self._lock, self._entry, self._inflight = Lock(), None, None

    def get_or_build(self, key, builder):
        while True:
            with self._lock:
                if self._entry and self._entry[0] == key and self.clock() < self._entry[1]:
                    return self._entry[2]
                leader = self._inflight is None
                if leader:
                    flight_key, future = key, Future()
                    self._inflight = flight_key, future
                    # Expired or different-context bodies are not served while
                    # rebuilding. Evict them first to bound resident memory.
                    self._entry = None
                else:
                    flight_key, future = self._inflight
            if not leader:
                try:
                    result = future.result()
                except Exception:
                    if flight_key == key:
                        raise
                    continue
                if flight_key == key:
                    return result
                continue
            try:
                result = builder()
            except BaseException as error:
                with self._lock:
                    self._inflight = None
                future.set_exception(error)
                raise
            with self._lock:
                self._entry = key, self.clock() + self.ttl_seconds, result
                self._inflight = None
            future.set_result(result)
            return result
