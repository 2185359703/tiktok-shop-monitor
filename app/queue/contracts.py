import json
from dataclasses import dataclass
from uuid import UUID

from app.models import TaskType


@dataclass(frozen=True)
class TaskMessage:
    task_id: str
    task_type: TaskType
    version: int = 1

    def to_bytes(self) -> bytes:
        return json.dumps({"version": 1, "task_id": self.task_id, "task_type": self.task_type.value}, separators=(",", ":")).encode()

    @classmethod
    def from_bytes(cls, value: bytes) -> "TaskMessage":
        try:
            if len(value) > 1024:
                raise ValueError
            data = json.loads(value)
            if set(data) != {"version", "task_id", "task_type"} or type(data["version"]) is not int or data["version"] != 1:
                raise ValueError
            identifier = str(UUID(data["task_id"]))
            if identifier != data["task_id"]:
                raise ValueError
            return cls(identifier, TaskType(data["task_type"]))
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ValueError("无效的任务通知；消息只能携带版本、任务 UUID 和任务类型。") from None
