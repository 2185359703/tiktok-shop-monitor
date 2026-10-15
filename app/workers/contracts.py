from dataclasses import dataclass
from collections.abc import Callable
from contextlib import AbstractContextManager
from threading import Event
from typing import Protocol

from sqlalchemy.orm import Session

from app.services.task_runtime import ExecutionResult, TaskLease
from app.settings import Settings


class RequestGate(Protocol):
    def wait_turn(self, stop: Event) -> None: ...
    def pause(self) -> None: ...
    def pause_remaining(self) -> float: ...


@dataclass(frozen=True)
class HandlerContext:
    settings: Settings
    gate: RequestGate
    stop: Event
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None


class TaskHandler(Protocol):
    def prepare(self, lease: TaskLease, context: HandlerContext) -> ExecutionResult:
        """Read/parse outside SQL; return a fenced transactional apply function."""
        ...
