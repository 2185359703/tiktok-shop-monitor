"""Inspect only timings and states, without printing SQL text or credentials."""

import json

from sqlalchemy import text

from app.settings import Settings
from app.storage.database import make_engine


engine = make_engine(Settings.load().database_url)
try:
    with engine.connect() as connection:
        rows = connection.execute(text("""SELECT ID, TIME, STATE, COMMAND, LEFT(INFO, 6) AS action
            FROM information_schema.PROCESSLIST WHERE DB=DATABASE() AND COMMAND<>'Sleep'
            AND ID<>CONNECTION_ID() ORDER BY TIME DESC""")).mappings()
        print(json.dumps({"business_data_read_only": True, "sessions": [dict(row) for row in rows]}, ensure_ascii=False))
finally:
    engine.dispose()
