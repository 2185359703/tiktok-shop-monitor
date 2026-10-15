"""Import validated CSV observations and initial shop-discovery tasks."""

import argparse
import csv
import json
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from app.services.seed_importer import import_seed_batch, read_seed_csv
from app.storage.database import PROJECT_ROOT, make_engine, make_session_factory, session_scope


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=PROJECT_ROOT / "data/seed/shops_us_clothing.csv")
    parser.add_argument("--dry-run", action="store_true", help="Validate the CSV without a DB connection.")
    args = parser.parse_args(argv)
    try:
        batch = read_seed_csv(args.file)
        if args.dry_run:
            result = {"mode": "validation_only", "input_rows": batch.input_rows,
                      "unique_rows": len(batch.rows), "duplicates_in_file": batch.duplicate_rows}
        else:
            engine = make_engine()
            try:
                with session_scope(make_session_factory(engine)) as session:
                    result = {"mode": "committed", **import_seed_batch(session, batch)}
            finally:
                engine.dispose()
        print(json.dumps({**result, "categories": batch.categories}, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, UnicodeError, csv.Error) as exc:
        print(f"种子导入失败：{exc}")
    except SQLAlchemyError as exc:
        codes = getattr(getattr(exc, "orig", None), "args", ())
        print(f"种子导入失败：{type(exc).__name__}，MySQL code {codes[0] if codes else 'unknown'}。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
