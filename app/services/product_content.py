"""Persist only the content extension table under the catalog's product lock."""

import re
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import inspect, select

from app.crawlers.product_content import ProductContentProjection
from app.crawlers.product_list import product_id_from_url
from app.crawlers.public_shop import PublicPageError, public_url
from app.models import Product, ProductContent
from app.queue.retry_policy import Failure, TaskFailure
from app.services.seed_importer import fingerprint


def product_content_table_available(session):
    return inspect(session.connection()).has_table(ProductContent.__tablename__)


def _invalid():
    return TaskFailure(Failure("product_content_identity_conflict", "介绍投影、观察时间与商品公开身份不一致，未写入内容。"))


def persist_product_content(session, product_id: int, projection: ProductContentProjection, *,
                            source_url: str, response_hash: str, observed_at: datetime) -> dict:
    """Never mutate Product, SKU, snapshots, baselines, or business versioning.

    Incomplete observations cannot erase earlier nonempty merchant content.
    Their failure/coverage remains visible in the enclosing task checkpoint.
    """
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
    if (product is None or product.platform != "tiktok_shop" or product.region != "US"
            or product_id_from_url(source_url) != product.platform_product_id
            or product_id_from_url(public_url(product.product_url)) != product.platform_product_id):
        raise _invalid()
    row = session.scalar(select(ProductContent).where(ProductContent.product_id == product_id).with_for_update())
    result = {"content_saved": False, "complete": projection.completeness == "complete",
              "completeness": projection.completeness, "source_version": projection.source_version,
              "content_nodes": len(projection.sections["description"]),
              "content_parameters": len(projection.sections["specifications"]),
              "unsupported_blocks": list(projection.unsupported_blocks)}
    if row is not None and row.observed_at > observed_at:
        return {**result, "stale_content_skipped": True}
    if row is not None and projection.completeness in {"missing", "empty", "partial"} and (row.sections.get("description") or row.sections.get("specifications")):
        return {**result, "retained_existing_content": True}
    payload = asdict(projection)
    content_hash = fingerprint(payload)
    if row is not None and row.observed_at == observed_at and row.content_hash != content_hash:
        return {**result, "same_observation_conflict_skipped": True}
    version = row.content_version + int(row.content_hash != content_hash) if row is not None else 1
    values = {**payload, "unsupported_blocks": list(projection.unsupported_blocks),
              "content_hash": content_hash, "content_version": version, "source_url": source_url,
              "response_hash": response_hash, "observed_at": observed_at}
    if row is None:
        row = ProductContent(product_id=product_id, **values)
        session.add(row)
    else:
        for field, value in values.items():
            setattr(row, field, value)
    session.flush()
    return {**result, "content_saved": True, "content_document_hash": content_hash, "content_document_version": version}

