from dataclasses import replace

import pytest
from sqlalchemy import select

from app.models import CrawlTask, SeedShopObservation, TaskStatus
from app.services.seed_importer import SeedBatch, import_seed_batch, normalize_seed
from app.services.seed_link_verification import sync_verified_store_link
from app.services.seed_replacements import retire_replaced_seed_tasks
from scripts.verify_seed_links import observation_anchors
from test_seed_links import SHOP_URL, raw_seed, storefront


def imported(session):
    row = normalize_seed(raw_seed(), 2)
    import_seed_batch(session, SeedBatch((row,), 1))
    return row


def test_enriched_csv_anchors_to_original_db_hash_and_identity(session):
    old = imported(session)
    sync_verified_store_link(session, old, storefront(), "source.json")
    new = normalize_seed({**raw_seed(), "shop_url": SHOP_URL, "source_url": SHOP_URL,
                         "verification_status": "verified_public_shop_page", "observed_at": "2026-10-13"}, 2)
    anchors, identities = observation_anchors(session.connection(), SeedBatch((new,), 1))
    assert new.row_hash != old.row_hash
    assert anchors[new.row_hash].row_hash == old.row_hash
    assert identities[new.row_hash] == "7495552971774921097"


def test_retirement_cancels_only_queued_task_and_preserves_observation(session):
    row = imported(session)
    task = session.scalar(select(CrawlTask))
    task.checkpoint = {"source_evidence": "keep"}
    session.flush()
    assert retire_replaced_seed_tasks(session, row.row_hash, active_shop_ids=set(), replacement_row_hash="b"*64) == 1
    assert task.status == TaskStatus.CANCELLED
    assert task.checkpoint["source_evidence"] == "keep"
    assert task.attempt_count == 0
    seed = session.scalar(select(SeedShopObservation))
    assert seed.row_hash == row.row_hash and seed.raw_record == raw_seed()
    assert retire_replaced_seed_tasks(session, row.row_hash, active_shop_ids=set(), replacement_row_hash="b"*64) == 0


def test_retirement_never_interrupts_running_worker(session):
    row = imported(session)
    task = session.scalar(select(CrawlTask))
    task.status = TaskStatus.RUNNING
    session.flush()
    with pytest.raises(ValueError, match="运行中"):
        retire_replaced_seed_tasks(session, row.row_hash, active_shop_ids=set(), replacement_row_hash="b"*64)
    assert task.status == TaskStatus.RUNNING


def test_reusing_prior_page_evidence_never_starts_task_before_creation(session):
    row = imported(session)
    sync_verified_store_link(session, row, storefront(), "source.json")
    task = session.scalar(select(CrawlTask).where(CrawlTask.resource_type == "seed"))
    assert task.started_at >= task.created_at
