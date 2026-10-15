"""Deterministic comparisons of adjacent valid immutable observations."""

from dataclasses import dataclass

from sqlalchemy import or_, and_, select
from sqlalchemy.dialects.mysql import insert

from app.models import ChangeEvent, ChangeEventType as EventType, Product, ProductSnapshot, ResourceType, StockStatus, VariantSnapshot
from app.queue.retry_policy import Failure, TaskFailure
from app.services.seed_importer import fingerprint
from app.services.snapshot_service import SNAPSHOT_FORMAT
from app.services.task_runtime import database_now


@dataclass(frozen=True)
class Change:
    event_type: EventType
    variant_id: int | None
    old_value: object
    new_value: object


def price_changes(old, new, variant_id=None, *, sku=False):
    if old.currency != new.currency:
        return []
    fields = ("price",) if sku else ("price_min", "price_max")
    before = [getattr(old, field) for field in fields]
    after = [getattr(new, field) for field in fields]
    # NULL -> known is enrichment, never a measured price drop or increase.
    if any(value is None for value in (*before, *after)) or before == after:
        return []
    return [Change(EventType.PRICE_CHANGED, variant_id,
                   {"currency": old.currency, **{field: str(value) for field, value in zip(fields, before)}},
                   {"currency": new.currency, **{field: str(value) for field, value in zip(fields, after)}})]


def stock_changes(before, after, variant_id=None):
    available = {StockStatus.IN_STOCK, StockStatus.PARTIAL}
    if before in available and after == StockStatus.OUT_OF_STOCK:
        return [Change(EventType.STOCKOUT, variant_id, {"stock_status": str(before)}, {"stock_status": str(after)})]
    if before == StockStatus.OUT_OF_STOCK and after in available:
        return [Change(EventType.RESTOCKED, variant_id, {"stock_status": str(before)}, {"stock_status": str(after)})]
    return []


def valid_projection(snapshot):
    raw = snapshot.raw_payload
    if not isinstance(raw, dict) or raw.get("snapshot_format") != SNAPSHOT_FORMAT:
        return False
    content, observation = raw.get("observed_content"), raw.get("observation")
    return (isinstance(content, dict) and isinstance(content.get("variant_options"), dict)
            and isinstance(content.get("attributes"), dict) and isinstance(observation, dict)
            and isinstance(observation.get("raw_attributes"), dict))


def compatible(previous, current):
    return (previous is not None and valid_projection(previous) and valid_projection(current)
            and previous.currency == current.currency and previous.pricing_context == current.pricing_context
            and previous.parser_version == current.parser_version)


def compare_observations(previous, current, previous_skus, current_skus):
    if not compatible(previous, current):
        return []  # First baseline, context drift or parser change is not a measured change.
    result = price_changes(previous, current) + stock_changes(previous.stock_status, current.stock_status)
    if previous.discount_rate is not None and current.discount_rate is not None:
        if previous.discount_rate == 0 and current.discount_rate > 0:
            result.append(Change(EventType.DISCOUNT_STARTED, None, {"discount_rate": str(previous.discount_rate)}, {"discount_rate": str(current.discount_rate)}))
        elif previous.discount_rate > 0 and current.discount_rate == 0:
            result.append(Change(EventType.DISCOUNT_ENDED, None, {"discount_rate": str(previous.discount_rate)}, {"discount_rate": str(current.discount_rate)}))
    for identifier in sorted(set(previous_skus) & set(current_skus)):
        old, new = previous_skus[identifier], current_skus[identifier]
        if old.currency != new.currency:
            continue
        result.extend(price_changes(old, new, identifier, sku=True))
        result.extend(stock_changes(old.stock_status, new.stock_status, identifier))
    old_content, new_content = previous.raw_payload["observed_content"], current.raw_payload["observed_content"]
    old_options, new_options = old_content["variant_options"], new_content["variant_options"]
    if old_options != new_options:
        # This means a changed observed specification set, not confirmed removals.
        result.append(Change(EventType.VARIANT_CHANGED, None,
                             {"observed_specifications": old_options}, {"observed_specifications": new_options}))
    before, after = {}, {}
    for field in ("title", "description", "original_category"):
        if field == "description" and (previous.raw_payload["observation"]["raw_attributes"].get("unsupported_description_blocks")
                                       or current.raw_payload["observation"]["raw_attributes"].get("unsupported_description_blocks")):
            continue
        old, new = old_content.get(field), new_content.get(field)
        if old is not None and new is not None and old != new:
            before[field], after[field] = old, new
    for field in sorted(set(old_content["attributes"]) & set(new_content["attributes"])):
        old, new = old_content["attributes"][field], new_content["attributes"][field]
        if old is not None and new is not None and old != new:
            before[field], after[field] = old, new
    if before:
        result.append(Change(EventType.CONTENT_CHANGED, None, before, after))
    return result


def detect_snapshot_changes(session, task):
    product = session.scalar(select(Product).where(Product.id == int(task.resource_id), Product.platform == task.platform,
                                                   Product.region == task.region).with_for_update())
    if product is None or task.resource_type != ResourceType.PRODUCT or task.payload.get("product_id") != str(product.id):
        raise TaskFailure(Failure("change_resource_conflict", "变化检测任务的商品归属不一致。"))
    try:
        snapshot_id = int(task.payload["snapshot_id"])
    except (KeyError, ValueError, TypeError):
        raise TaskFailure(Failure("invalid_change_snapshot", "变化检测任务缺少有效快照标识。")) from None
    current = session.scalar(select(ProductSnapshot).where(ProductSnapshot.id == snapshot_id,
                         ProductSnapshot.product_id == product.id))
    if (current is None or current.source_status != 200 or not valid_projection(current)
            or task.payload.get("pricing_context") != current.pricing_context):
        raise TaskFailure(Failure("invalid_change_snapshot", "快照来源、格式或价格上下文不可比较。"))
    previous = session.scalar(select(ProductSnapshot).where(
        ProductSnapshot.product_id == product.id, ProductSnapshot.pricing_context == current.pricing_context,
        ProductSnapshot.source_status == 200,
        or_(ProductSnapshot.captured_at < current.captured_at,
            and_(ProductSnapshot.captured_at == current.captured_at, ProductSnapshot.id < current.id)),
    ).order_by(ProductSnapshot.captured_at.desc(), ProductSnapshot.id.desc()).limit(1))
    previous_skus = {row.variant_id: row for row in session.scalars(select(VariantSnapshot).where(
        VariantSnapshot.product_snapshot_id == previous.id))} if previous else {}
    current_skus = {row.variant_id: row for row in session.scalars(select(VariantSnapshot).where(
        VariantSnapshot.product_snapshot_id == current.id))}
    changes = compare_observations(previous, current, previous_skus, current_skus)
    now, rows = database_now(session), []
    for change in changes:
        key = fingerprint({"rule_version": 1, "product_id": str(product.id), "variant_id": str(change.variant_id) if change.variant_id is not None else None,
                           "snapshot_id": str(current.id), "previous_snapshot_id": str(previous.id) if previous else None,
                           "event_type": change.event_type.value})
        rows.append({"event_hash": key, "product_id": product.id, "variant_id": change.variant_id,
                     "snapshot_id": current.id, "previous_snapshot_id": previous.id if previous else None,
                     "event_type": change.event_type, "old_value": change.old_value, "new_value": change.new_value, "detected_at": now})
    existing = set(session.scalars(select(ChangeEvent.event_hash).where(ChangeEvent.event_hash.in_([row["event_hash"] for row in rows]))))
    if rows:
        statement = insert(ChangeEvent).values(rows)
        session.execute(statement.on_duplicate_key_update(event_hash=statement.inserted.event_hash))
    session.flush()
    return {"snapshot_id": str(current.id), "previous_snapshot_id": str(previous.id) if previous else None,
            "baseline": previous is None, "comparison_skipped": previous is not None and not compatible(previous, current),
            "events_detected": len(rows), "events_written": len(rows) - len(existing)}
