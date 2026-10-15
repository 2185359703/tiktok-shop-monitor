"""Read actual photo progress and inspect only the task aggregate's query plan."""

import json
import time

from sqlalchemy import event

from app.services.image_backfill import fixed_image_seed_batch, image_backfill_status
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory


settings = Settings.load()
engine = make_engine(settings.database_url)
def explain(connection, cursor, statement, parameters, context, executemany):
    if statement.lstrip().startswith("SELECT") and "crawl_tasks" in statement and "GROUP BY" in statement:
        cursor.execute("EXPLAIN " + statement, parameters)
        names = [column[0] for column in cursor.description]
        print(json.dumps({"task_query_plan": [dict(zip(names, row)) for row in cursor.fetchall()]}, default=str), flush=True)
event.listen(engine, "before_cursor_execute", explain)
began = time.monotonic()
try:
    with make_session_factory(engine)() as session:
        result = image_backfill_status(session, settings, fixed_image_seed_batch())
    print(json.dumps({"elapsed_seconds": round(time.monotonic() - began, 3),
        "status": result["status"], "tasks_total": result["tasks_total"], "succeeded": result["task_statuses"]["succeeded"],
        "dead": result["dead"], "products_with_current_images": result["products_with_current_images"]}, ensure_ascii=False))
finally:
    engine.dispose()
