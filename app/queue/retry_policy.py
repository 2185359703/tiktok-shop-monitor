from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True)
class Failure:
    code: str
    message: str
    retryable: bool = False
    delay_seconds: float | None = None
    response_status: int | None = None
    request_url: str | None = None
    response_hash: str | None = None
    response_bytes: int | None = None
    evidence_path: str | None = None


class TaskFailure(Exception):
    def __init__(self, failure: Failure):
        self.failure = failure
        super().__init__(failure.message)


def retry_delay(task_id: str, attempt: int, base: int, maximum: int) -> float:
    delay = min(maximum, base * 2 ** min(max(attempt - 1, 0), 20))
    jitter = int(sha256(f"{task_id}:{attempt}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return min(maximum, delay * (1 + jitter * 0.2))
