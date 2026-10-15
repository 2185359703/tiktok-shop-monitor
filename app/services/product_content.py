"""Persist content separately, with honest provenance for retained sections."""

import json
import re
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import inspect, select

from app.crawlers.product_content import ProductContentProjection, _description_text
from app.crawlers.product_list import product_id_from_url
from app.crawlers.public_shop import PublicPageError, public_url
from app.models import Product, ProductContent
from app.queue.retry_policy import Failure, TaskFailure
from app.services.seed_importer import fingerprint


SECTIONS = ("specifications", "description")
SOURCE_FIELDS = {"specifications": "specifications", "description": "desc_blocks"}
PROVENANCE = "_observations"


def product_content_table_available(session):
    return inspect(session.connection()).has_table(ProductContent.__tablename__)


def _invalid():
    return TaskFailure(Failure("product_content_identity_conflict", "介绍投影、观察时间与商品公开身份不一致，未写入内容。"))


def _time(value):
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _metadata(source_url, response_hash, observed_at, source_version, completeness):
    return {"source_url": source_url, "response_hash": response_hash, "observed_at": observed_at.isoformat(),
            "source_version": source_version, "completeness": completeness}


def _incoming_metadata(projection, source_url, response_hash, observed_at):
    base = _metadata(source_url, response_hash, observed_at, projection.source_version, projection.completeness)
    latest = {**base, "sections": dict(projection.source_shape["sections"]),
              "unsupported_blocks": list(projection.unsupported_blocks)}
    sections = {section: {**base, "completeness": projection.source_shape["sections"][section]} for section in SECTIONS}
    return {"latest": latest, "sections": sections}


def stored_content_projection(row):
    shape = {key: value for key, value in row.source_shape.items() if key != PROVENANCE}
    return ProductContentProjection.from_record({"source_version": row.source_version, "sections": row.sections,
        "description_text": row.description_text, "completeness": row.completeness,
        "unsupported_blocks": row.unsupported_blocks, "source_shape": shape})


def _stored_metadata(row):
    """Legacy rows predate section provenance; their original time is retained."""
    stored_shape = row.source_shape if isinstance(row.source_shape, dict) else {}
    states = stored_shape.get("sections", {})
    if not isinstance(states, dict):
        states = {}
    states = {section: states.get(section) if isinstance(states.get(section), str) and states[section] in {"missing", "empty", "partial", "complete"} else "partial" for section in SECTIONS}
    base = _metadata(public_url(row.source_url), row.response_hash, row.observed_at, row.source_version, row.completeness)
    fallback = {"latest": {**base, "sections": states, "unsupported_blocks": list(row.unsupported_blocks or [])},
                "sections": {section: {**base, "completeness": states[section]} for section in SECTIONS}}
    saved = stored_shape.get(PROVENANCE)
    if not isinstance(saved, dict) or set(saved) != {"latest", "sections"} or not isinstance(saved["sections"], dict) or set(saved["sections"]) != set(SECTIONS):
        return fallback
    required_keys = {"source_url", "response_hash", "observed_at", "source_version", "completeness"}
    entries = (saved.get("latest"), *saved["sections"].values())
    for index, value in enumerate(entries):
        if (not isinstance(value, dict) or _time(value.get("observed_at")) is None
                or set(value) != required_keys | ({"sections", "unsupported_blocks"} if index == 0 else set())
                or not isinstance(value.get("completeness"), str)
                or value["completeness"] not in {"missing", "empty", "partial", "complete"}
                or not isinstance(value.get("response_hash"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", value["response_hash"])
                or not isinstance(value.get("source_version"), str)):
            return fallback
        if index == 0:
            if (not isinstance(value["sections"], dict) or set(value["sections"]) != set(SECTIONS)
                    or any(not isinstance(state, str) or state not in {"missing", "empty", "partial", "complete"} for state in value["sections"].values())
                    or not isinstance(value["unsupported_blocks"], list)
                    or any(not isinstance(issue, str) or not re.fullmatch(r"[A-Za-z0-9_:.-]{1,128}", issue) for issue in value["unsupported_blocks"])):
                return fallback
        try:
            if product_id_from_url(public_url(value.get("source_url"))) != product_id_from_url(row.source_url):
                return fallback
        except (TypeError, ValueError, PublicPageError):
            return fallback
    return json.loads(json.dumps(saved))


def content_observation_metadata(row):
    """Public API metadata excludes internal response hashes and arbitrary JSON."""
    metadata = _stored_metadata(row)
    public_keys = ("observed_at", "completeness", "source_url", "source_version")
    latest = {key: metadata["latest"][key] for key in public_keys}
    latest["sections"] = {section: metadata["latest"].get("sections", {}).get(section, "missing") for section in SECTIONS}
    latest["unsupported_blocks"] = list(metadata["latest"].get("unsupported_blocks", []))
    return {"section_observations": {section: {key: metadata["sections"][section][key] for key in public_keys} for section in SECTIONS},
            "latest_observation": latest}


def _supplement(existing, incoming, metadata, incoming_metadata):
    """Add evidence without guessing how unrelated description fragments fit."""
    sections = json.loads(json.dumps(existing.sections))
    shape = json.loads(json.dumps(existing.source_shape))
    changed = []
    for section in SECTIONS:
        old, new = sections[section], incoming.sections[section]
        old_state = shape["sections"][section]
        new_state = incoming.source_shape["sections"][section]
        replace_section = bool(new) and (not old or old_state == "partial" and new_state == "complete")
        if section == "description" and old and len(new) > len(old) and new[:len(old)] == old:
            replace_section = True
        if replace_section:
            sections[section] = new
            shape[SOURCE_FIELDS[section]] = incoming.source_shape[SOURCE_FIELDS[section]]
            shape["sections"][section] = new_state
            metadata["sections"][section] = incoming_metadata["sections"][section]
            changed.append(section)
        elif section == "specifications" and new:
            names = {row["name"].casefold() for row in old}
            additions = [row for row in new if row["name"].casefold() not in names]
            if additions:
                sections[section] = old + additions
                shape["sections"][section] = "partial"
                shape["specifications"] = {"present": True, "kind": "list", "count": len(sections[section]), "types": {}}
                metadata["sections"][section]["completeness"] = "partial"
                changed.append(section)
    if not changed:
        return None, ()
    issues = sorted(set(existing.unsupported_blocks) | set(incoming.unsupported_blocks) | {"merge:mixed_observations"})
    shape["issues"] = issues
    merged = ProductContentProjection.from_record({"source_version": incoming.source_version,
        "sections": sections, "description_text": _description_text(sections["description"]),
        "completeness": "partial", "unsupported_blocks": issues, "source_shape": shape})
    return merged, tuple(changed)


def persist_product_content(session, product_id: int, projection: ProductContentProjection, *,
                            source_url: str, response_hash: str, observed_at: datetime) -> dict:
    """Write only content; mixed observations keep each section's original age."""
    if type(product_id) is not int or product_id <= 0 or not isinstance(projection, ProductContentProjection):
        raise _invalid()
    try:
        projection = ProductContentProjection.from_record(asdict(projection))
        source_url = public_url(source_url)
        if (not isinstance(response_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", response_hash)
                or not isinstance(observed_at, datetime) or observed_at.tzinfo is None):
            raise ValueError
    except (TypeError, ValueError, PublicPageError):
        raise _invalid() from None
    product = session.scalar(select(Product).where(Product.id == product_id).with_for_update()
                             .execution_options(populate_existing=True))
    try:
        valid = (product is not None and product.platform == "tiktok_shop" and product.region == "US"
            and product_id_from_url(source_url) == product.platform_product_id
            and product_id_from_url(public_url(product.product_url)) == product.platform_product_id)
    except (TypeError, ValueError, PublicPageError):
        valid = False
    if not valid:
        raise _invalid()
    row = session.scalar(select(ProductContent).where(ProductContent.product_id == product_id).with_for_update())
    result = {"content_saved": False, "complete": projection.completeness == "complete",
              "completeness": projection.completeness, "observed_completeness": projection.completeness,
              "source_version": projection.source_version, "content_nodes": len(projection.sections["description"]),
              "content_parameters": len(projection.sections["specifications"]), "unsupported_blocks": list(projection.unsupported_blocks)}
    incoming_metadata = _incoming_metadata(projection, source_url, response_hash, observed_at)
    metadata = _stored_metadata(row) if row is not None else incoming_metadata
    retained = ()
    if row is not None:
        newest_content = max(_time(value["observed_at"]) for section, value in metadata["sections"].items() if row.sections[section]) if any(row.sections.values()) else row.observed_at
        if newest_content > observed_at:
            return {**result, "stale_content_skipped": True}
        if newest_content == observed_at and row.content_hash != fingerprint(asdict(projection)):
            return {**result, "same_observation_conflict_skipped": True}
        if _time(metadata["latest"]["observed_at"]) <= observed_at:
            metadata["latest"] = incoming_metadata["latest"]
        if projection.completeness != "complete" and any(row.sections.values()):
            candidate = None
            if row.completeness == "partial" and projection.completeness == "partial":
                try:
                    proposed_metadata = json.loads(json.dumps(metadata))
                    candidate, changed = _supplement(stored_content_projection(row), projection, proposed_metadata, incoming_metadata)
                    if candidate is not None:
                        metadata = proposed_metadata
                except (ValueError, TypeError):
                    # Optional enrichment must not fail the valid primary detail
                    # transaction when a combined document exceeds its budget.
                    changed = ()
                    result["supplement_limits_reached"] = True
                retained = tuple(section for section in SECTIONS if section not in changed and row.sections[section])
            if candidate is None:
                row.source_shape = {**row.source_shape, PROVENANCE: metadata}
                session.flush()
                return {**result, "retained_existing_content": True, "observation_recorded": True,
                        "stored_completeness": row.completeness, "content_observed_at": row.observed_at.isoformat()}
            projection = candidate
        else:
            metadata["sections"] = incoming_metadata["sections"]
    payload = asdict(projection)
    document_hash = fingerprint(payload)
    if row is not None and row.observed_at == observed_at and row.content_hash != document_hash:
        return {**result, "same_observation_conflict_skipped": True}
    version = row.content_version + int(row.content_hash != document_hash) if row is not None else 1
    relevant = [metadata["sections"][section] for section in SECTIONS if projection.sections[section]]
    oldest = min(relevant, key=lambda value: _time(value["observed_at"])) if relevant else metadata["latest"]
    values = {**payload, "source_shape": {**payload["source_shape"], PROVENANCE: metadata},
              "unsupported_blocks": list(projection.unsupported_blocks),
              "content_hash": document_hash, "content_version": version, "source_url": oldest["source_url"],
              "response_hash": oldest["response_hash"], "observed_at": _time(oldest["observed_at"])}
    if row is None:
        row = ProductContent(product_id=product_id, **values)
        session.add(row)
    else:
        for field, value in values.items():
            setattr(row, field, value)
    session.flush()
    return {**result, "content_saved": True, "complete": projection.completeness == "complete",
            "completeness": projection.completeness, "retained_sections": list(retained),
            "content_document_hash": document_hash, "content_document_version": version,
            "content_observed_at": row.observed_at.isoformat()}
