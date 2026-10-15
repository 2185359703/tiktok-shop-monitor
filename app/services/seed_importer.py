"""Validate seed observations and enqueue one initial identity task per row."""

import csv
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.orm import Session

from app.models import (
    CrawlTask, ResolutionStatus, ResourceType, SeedShopObservation, SeedType,
    TaskStatus, TaskType,
)


PLATFORM = "tiktok_shop"
CSV_FIELDS = (
    "region", "category", "seed_type", "seed_value", "shop_url", "source_url",
    "verification_status", "observed_at",
)


def fingerprint(value: dict) -> str:
    """Stable UTF-8 JSON: sorted keys, compact separators, no ASCII escaping."""
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SeedRow:
    normalized: dict[str, str | None]
    raw_record: dict[str, str]
    row_hash: str

    def database_values(self) -> dict:
        values = {"platform": PLATFORM, **self.normalized}
        values["observed_at"] = date.fromisoformat(str(values["observed_at"]))
        return {
            **values, "row_hash": self.row_hash, "raw_record": self.raw_record,
            "resolution_status": ResolutionStatus.PENDING,
        }


@dataclass(frozen=True)
class SeedBatch:
    rows: tuple[SeedRow, ...]
    input_rows: int

    @property
    def duplicate_rows(self) -> int:
        return self.input_rows - len(self.rows)

    @property
    def categories(self) -> dict[str, int]:
        return dict(sorted(Counter(str(row.normalized["category"]) for row in self.rows).items()))


def validate_web_url(value: str, field: str, line: int) -> None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or any(character.isspace() for character in value)
        ):
            raise ValueError
    except ValueError:
        # Do not echo potentially sensitive input URLs.
        raise ValueError(f"CSV 第 {line} 行：{field} 必须是无凭据的 HTTP(S) URL。") from None


def normalize_seed(raw: dict[str, str], line: int) -> SeedRow:
    values: dict[str, str | None] = {key: raw[key].strip() for key in CSV_FIELDS}
    values["region"] = str(values["region"]).upper()
    if not re.fullmatch(r"[A-Z]{2}", str(values["region"])):
        raise ValueError(f"CSV 第 {line} 行：region 必须是两位市场代码。")
    limits = {"category": 64, "seed_value": 255, "verification_status": 64}
    for field, maximum in limits.items():
        if not values[field] or len(str(values[field])) > maximum:
            raise ValueError(f"CSV 第 {line} 行：{field} 必填且不能超过 {maximum} 字符。")
    try:
        SeedType(str(values["seed_type"]))
    except ValueError:
        raise ValueError(f"CSV 第 {line} 行：seed_type 不受支持。") from None
    try:
        observed = date.fromisoformat(str(values["observed_at"]))
        if observed.isoformat() != values["observed_at"]:
            raise ValueError
    except ValueError:
        raise ValueError(f"CSV 第 {line} 行：observed_at 必须是 YYYY-MM-DD。") from None
    if not values["source_url"]:
        raise ValueError(f"CSV 第 {line} 行：source_url 必填。")
    validate_web_url(str(values["source_url"]), "source_url", line)
    values["shop_url"] = values["shop_url"] or None
    if values["shop_url"]:
        validate_web_url(str(values["shop_url"]), "shop_url", line)
    if values["seed_type"] == SeedType.SHOP_URL:
        validate_web_url(str(values["seed_value"]), "seed_value", line)
    return SeedRow(values, dict(raw), fingerprint({"platform": PLATFORM, **values}))


def read_seed_csv(path: Path) -> SeedBatch:
    """Validate the entire file before any transaction; retain untrimmed raw rows."""
    unique: dict[str, SeedRow] = {}
    count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        headers = reader.fieldnames or []
        if len(headers) != len(CSV_FIELDS) or set(headers) != set(CSV_FIELDS):
            raise ValueError("CSV 表头必须包含且仅包含约定的 8 个字段，不能重复。")
        for raw in reader:
            count += 1
            if None in raw or any(value is None for value in raw.values()):
                raise ValueError(f"CSV 第 {reader.line_num} 行：字段数量与表头不一致。")
            row = normalize_seed(raw, reader.line_num)
            unique.setdefault(row.row_hash, row)
    if not count:
        raise ValueError("CSV 没有种子记录。")
    # Consistent lock order across import processes.
    return SeedBatch(tuple(unique[key] for key in sorted(unique)), count)


def seed_task_key(row_hash: str) -> str:
    return fingerprint({
        "platform": PLATFORM, "task_type": "shop_discovery",
        "seed_row_hash": row_hash, "context": "initial_identity_resolution",
    })


def ensure_seed_task(session: Session, seed: SeedShopObservation, now: datetime) -> bool:
    """Caller holds the seed row lock. Never reopen or overwrite an existing task."""
    key = seed_task_key(seed.row_hash)
    existing = session.scalar(select(CrawlTask.id).where(
        CrawlTask.idempotency_key == key
    ).with_for_update())
    if existing is not None or seed.resolution_status != ResolutionStatus.PENDING:
        return False
    session.add(CrawlTask(
        platform=seed.platform, region=seed.region,
        idempotency_key=key, task_type=TaskType.SHOP_DISCOVERY,
        resource_type=ResourceType.SEED, resource_id=str(seed.id),
        payload={
            "schema_version": 1, "seed_id": str(seed.id), "seed_row_hash": seed.row_hash,
            "seed_type": seed.seed_type.value, "seed_value": seed.seed_value,
            "category": seed.category, "shop_url": seed.shop_url,
            "source_url": seed.source_url, "observed_at": seed.observed_at.isoformat(),
        },
        status=TaskStatus.PENDING, priority=5,
        scheduled_for=now, available_at=now,
    ))
    session.flush()
    return True


def import_seed_batch(session: Session, batch: SeedBatch) -> dict[str, int]:
    """Atomic with the caller's transaction; MySQL unique keys protect replays."""
    now = datetime.now(timezone.utc)
    hashes = [row.row_hash for row in batch.rows]
    before = set(session.scalars(select(SeedShopObservation.row_hash).where(
        SeedShopObservation.row_hash.in_(hashes)
    )))
    statement = insert(SeedShopObservation).values([row.database_values() for row in batch.rows])
    # A duplicate is deliberately a no-op: preserve original evidence, resolution
    # status, errors, mappings, and the corresponding task's execution state.
    session.execute(statement.on_duplicate_key_update(row_hash=statement.inserted.row_hash))
    seeds = session.scalars(select(SeedShopObservation).where(
        SeedShopObservation.row_hash.in_(hashes)
    ).order_by(SeedShopObservation.row_hash).with_for_update()).all()
    created = sum(ensure_seed_task(session, seed, now) for seed in seeds)
    return {
        "input_rows": batch.input_rows, "unique_rows": len(batch.rows),
        "duplicates_in_file": batch.duplicate_rows,
        "new_observations": len(hashes) - len(before), "existing_observations": len(before),
        "new_discovery_tasks": created,
    }
