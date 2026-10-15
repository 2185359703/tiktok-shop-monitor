import csv
import io
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import CrawlTask, ResolutionStatus, SeedShopObservation, TaskStatus
from app.services.seed_importer import (
    CSV_FIELDS, SeedBatch, fingerprint, import_seed_batch, normalize_seed, read_seed_csv,
    seed_task_key,
)
from scripts.seed_shops import main


def raw_seed(**overrides):
    return {
        "region": "US", "category": "women_clothing", "seed_type": "shop_name",
        "seed_value": "CHOUYATOU", "shop_url": "",
        "source_url": "https://shop.tiktok.com/us/k/clothing-for-women",
        "verification_status": "observed_public_category_page", "observed_at": "2026-09-25",
        **overrides,
    }


def write_csv(tmp_path, rows, headers=CSV_FIELDS):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    path = tmp_path / "seeds.csv"
    path.write_text(stream.getvalue(), encoding="utf-8-sig")
    return path


def batch_for(*rows):
    return SeedBatch(tuple(normalize_seed(row, index + 2) for index, row in enumerate(rows)), len(rows))


def test_original_seed_archive_validates_without_database():
    path = Path(__file__).resolve().parents[1] / "data/seed/archive/shops_us_clothing_names_20260925.csv"
    batch = read_seed_csv(path)
    assert batch.input_rows == len(batch.rows) == 100
    assert batch.duplicate_rows == 0
    assert len(batch.categories) == 6
    assert all(row.normalized["shop_url"] is None for row in batch.rows)


def test_active_seed_list_keeps_a_valid_schema_and_traceable_store_urls():
    from app.crawlers.public_shop import shop_id_from_url
    path = Path(__file__).resolve().parents[1] / "data/seed/shops_us_clothing.csv"
    batch = read_seed_csv(path)
    assert batch.input_rows == len(batch.rows) == 100
    for row in batch.rows:
        if row.normalized["shop_url"]:
            assert shop_id_from_url(str(row.normalized["shop_url"]))
            assert row.normalized["verification_status"] == "verified_public_shop_page"


def test_active_pool_has_100_distinct_links_and_only_approved_name_replacements():
    import json
    from app.crawlers.public_shop import shop_id_from_url
    root = Path(__file__).resolve().parents[1]
    active = read_seed_csv(root / "data/seed/shops_us_clothing.csv")
    original = read_seed_csv(root / "data/seed/archive/shops_us_clothing_names_20260925.csv")
    plan = json.loads((root / "data/seed/shop_replacements_20261013.json").read_text(encoding="utf-8"))
    assert len({shop_id_from_url(str(row.normalized["shop_url"])) for row in active.rows}) == 100
    assert all(row.normalized["shop_url"] for row in active.rows)
    assert active.categories == original.categories
    old_names = {str(row.normalized["seed_value"]) for row in original.rows}
    new_names = {str(row.normalized["seed_value"]) for row in active.rows}
    assert len(old_names & new_names) == 78
    assert old_names - new_names == {item["old_seed_value"] for item in plan["replacements"]}
    assert new_names - old_names == {item["new_row"]["seed_value"] for item in plan["replacements"]}


def test_row_hash_normalization_preserves_raw_and_name_case():
    row = normalize_seed(raw_seed(region=" us ", seed_value=" CHOUYATOU ", shop_url=" "), 2)
    expected = raw_seed()
    expected["shop_url"] = None
    assert row.row_hash == fingerprint({"platform": "tiktok_shop", **expected})
    assert row.raw_record["seed_value"] == " CHOUYATOU "
    assert row.database_values()["observed_at"] == date(2026, 9, 25)
    assert row.row_hash != normalize_seed(raw_seed(seed_value="chouyatou"), 2).row_hash


def test_csv_deduplicates_normalized_observations(tmp_path):
    path = write_csv(tmp_path, [raw_seed(), raw_seed(region=" us ", shop_url=" ")])
    batch = read_seed_csv(path)
    assert batch.input_rows == 2 and len(batch.rows) == 1 and batch.duplicate_rows == 1


@pytest.mark.parametrize("field,value", [
    ("region", "USA"), ("category", ""), ("seed_value", "x" * 256),
    ("seed_type", "unknown"), ("observed_at", "20260925"),
    ("observed_at", "2026-02-30"), ("source_url", ""),
    ("source_url", "https://user:secret@example.test/path"),
    ("shop_url", "javascript:alert(1)"),
])
def test_invalid_row_fails_with_field_not_input_values(field, value):
    with pytest.raises(ValueError) as error:
        normalize_seed(raw_seed(**{field: value}), 9)
    assert "9" in str(error.value)
    assert "secret" not in str(error.value)


def test_header_and_truncated_row_rejected_before_import(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("region,region\nUS,US\n", encoding="utf-8")
    with pytest.raises(ValueError, match="表头"):
        read_seed_csv(path)
    path.write_text(",".join(CSV_FIELDS) + "\nUS,women_clothing\n", encoding="utf-8")
    with pytest.raises(ValueError, match="字段数量"):
        read_seed_csv(path)


def test_dry_run_never_connects(tmp_path, monkeypatch, capsys):
    path = write_csv(tmp_path, [raw_seed()])
    monkeypatch.setattr("scripts.seed_shops.make_engine", lambda: pytest.fail("dry-run connected"))
    assert main(["--file", str(path), "--dry-run"]) == 0
    assert "validation_only" in capsys.readouterr().out


def test_import_replay_preserves_observation_and_task_state(session):
    batch = batch_for(raw_seed())
    first = import_seed_batch(session, batch)
    seed = session.scalar(select(SeedShopObservation))
    task = session.scalar(select(CrawlTask))
    original_uuid = task.task_id
    original_schedule = task.scheduled_for
    task.status = TaskStatus.RETRY_WAITING
    task.attempt_count = 1
    task.last_error = "keep this execution evidence"
    task.checkpoint = {"cursor": "keep"}
    task.available_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    seed.resolution_error = "keep original resolution evidence"
    session.flush()
    repeated = import_seed_batch(session, batch)
    assert first["new_observations"] == first["new_discovery_tasks"] == 1
    assert repeated["new_observations"] == repeated["new_discovery_tasks"] == 0
    session.refresh(seed)
    session.refresh(task)
    assert seed.resolution_error == "keep original resolution evidence"
    assert task.task_id == original_uuid and task.scheduled_for == original_schedule
    assert task.status == TaskStatus.RETRY_WAITING and task.attempt_count == 1
    assert task.checkpoint == {"cursor": "keep"}
    assert task.last_error == "keep this execution evidence"
    assert session.scalar(select(func.count()).select_from(CrawlTask)) == 1


def test_resolved_seed_is_not_reopened_and_missing_pending_task_is_repaired(session):
    batch = batch_for(raw_seed(), raw_seed(seed_value="AUTOMET"))
    import_seed_batch(session, batch)
    seeds = session.scalars(select(SeedShopObservation).order_by(SeedShopObservation.id)).all()
    for seed in seeds:
        task = session.scalar(select(CrawlTask).where(CrawlTask.idempotency_key == seed_task_key(seed.row_hash)))
        session.delete(task)
    seeds[0].resolution_status = ResolutionStatus.RESOLVED
    session.flush()
    result = import_seed_batch(session, batch)
    assert result["new_discovery_tasks"] == 1
    assert seeds[0].resolution_status == ResolutionStatus.RESOLVED


def test_import_transaction_can_roll_back_all_rows_and_tasks(session):
    with pytest.raises(RuntimeError):
        with session.begin_nested():
            import_seed_batch(session, batch_for(raw_seed()))
            raise RuntimeError("simulate failed operation")
    assert session.scalar(select(func.count()).select_from(SeedShopObservation)) == 0
    assert session.scalar(select(func.count()).select_from(CrawlTask)) == 0


def test_new_date_and_category_create_new_observations_not_fake_shop_ids(session):
    batch = batch_for(raw_seed(), raw_seed(observed_at="2026-09-26"), raw_seed(category="sportswear"))
    result = import_seed_batch(session, batch)
    assert result["new_observations"] == result["new_discovery_tasks"] == 3
    tasks = session.scalars(select(CrawlTask)).all()
    assert len({task.idempotency_key for task in tasks}) == 3
    assert all(task.payload["seed_id"] == task.resource_id for task in tasks)
