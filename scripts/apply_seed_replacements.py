"""Atomically sync the user-approved, independently verified 22 replacements."""

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.crawlers.public_shop import PublicPageSnapshot, shop_id_from_url
from app.models import Shop
from app.services.seed_importer import SeedBatch, import_seed_batch, normalize_seed, read_seed_csv
from app.services.seed_link_verification import sync_verified_store_link
from app.services.seed_replacements import retire_replaced_seed_tasks
from app.storage.database import PROJECT_ROOT, make_engine, make_session_factory, session_scope


def main() -> int:
    plan = json.loads((PROJECT_ROOT / "data/seed/shop_replacements_20261013.json").read_text(encoding="utf-8"))
    mappings = plan["replacements"]
    original = read_seed_csv(PROJECT_ROOT / "data/seed/archive/shops_us_clothing_names_20260925.csv")
    originals_by_key = {(row.normalized["category"], row.normalized["seed_value"]): row for row in original.rows}
    rows = tuple(normalize_seed(item["new_row"], 1) for item in mappings)
    active = read_seed_csv(PROJECT_ROOT / "data/seed/shops_us_clothing.csv")
    active_platform_ids = {shop_id_from_url(str(row.normalized["shop_url"])) for row in active.rows}
    if len(rows) != 22 or len(active_platform_ids) != 100 or None in active_platform_ids:
        raise ValueError("替换方案或当前名单不满足22条替换、100个有效独立店铺ID。")
    engine = make_engine()
    try:
        factory = make_session_factory(engine)
        with session_scope(factory) as session:
            result = import_seed_batch(session, SeedBatch(rows, len(rows)))
            synced = 0
            for mapping, row in zip(mappings, rows):
                evidence = json.loads((PROJECT_ROOT / mapping["evidence_path"]).read_text(encoding="utf-8"))
                outcome = sync_verified_store_link(session, row, PublicPageSnapshot.from_evidence(evidence),
                                                  mapping["evidence_path"])
                if outcome not in {"resolved", "url_enriched"}:
                    raise ValueError(f"替换身份不能安全同步：{outcome}。")
                synced += 1
            active_internal_ids = set(session.scalars(select(Shop.id).where(
                Shop.platform == "tiktok_shop", Shop.region == "US",
                Shop.platform_shop_id.in_(active_platform_ids),
            )))
            if len(active_internal_ids) != 100:
                raise ValueError("数据库尚未包含完整的100个当前店铺身份。")
            retired = 0
            for mapping, row in zip(mappings, rows):
                old = originals_by_key[(mapping["category"], mapping["old_seed_value"])]
                retired += retire_replaced_seed_tasks(session, old.row_hash,
                    active_shop_ids=active_internal_ids, replacement_row_hash=row.row_hash)
        print(json.dumps({**result, "verified_replacement_rows": synced, "retired_unclaimed_tasks": retired},
                         ensure_ascii=False, indent=2))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SQLAlchemyError as exc:
        codes = getattr(getattr(exc, "orig", None), "args", ())
        print(f"替换同步失败：{type(exc).__name__}，MySQL code {codes[0] if codes else 'unknown'}。")
        raise SystemExit(1) from None
    except (ValueError, OSError, KeyError) as exc:
        print(f"替换同步失败：{exc}")
        raise SystemExit(1) from None
