from dataclasses import dataclass

from app.settings import Settings


@dataclass(frozen=True)
class HandlerContext:
    settings: Settings
    gate: object
    database: object
    stop: object = None

    @property
    def session_factory(self):
        return self.database.sync_factory if self.database is not None else None
