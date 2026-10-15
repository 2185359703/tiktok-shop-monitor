"""Idempotent photo persistence inside the existing collection transaction."""

from dataclasses import asdict

from sqlalchemy import case, func, inspect, select

from app.crawlers.product_images import IMAGE_SOURCE_VERSION, ImageCandidate
from app.models.product_image import ProductImage


def persist_product_images(session, product_id, images, *, source_url, source_kind, observed_at):
    if source_kind not in {"detail", "listing"}:
        raise ValueError("未知图片来源。")
    if not images:
        # Old projections and partial pages contain no images; preserve earlier evidence.
        return 0
    if any(not isinstance(image, ImageCandidate) for image in images):
        raise ValueError("图片必须来自已校验的公开字段。")
    for image in images:
        ImageCandidate.from_record(asdict(image))
    rows = session.scalars(select(ProductImage).where(ProductImage.product_id == product_id)
                           .with_for_update()).all()
    latest = max((row.observed_at for row in rows if row.source_kind == source_kind), default=None)
    if latest is not None and latest > observed_at:
        return 0
    by_key = {row.image_key: row for row in rows}
    authoritative = source_kind == "detail"
    # A listing cover must not replace or expand an already observed detail gallery.
    if not authoritative and any(row.source_kind == "detail" for row in rows):
        return 0
    if authoritative:
        seen = {image.image_key for image in images}
        for row in rows:
            if row.image_key not in seen:
                row.is_current = False
    saved = 0
    for image in images:
        row = by_key.get(image.image_key)
        values = {"image_url": image.image_url, "thumbnail_url": image.thumbnail_url,
                  "source_url": source_url, "source_kind": source_kind, "source_version": IMAGE_SOURCE_VERSION,
                  "role": image.role, "position": image.position, "is_current": True, "observed_at": observed_at}
        if row is None:
            session.add(ProductImage(product_id=product_id, image_key=image.image_key, **values))
        else:
            for field, value in values.items():
                setattr(row, field, value)
        saved += 1
    session.flush()
    from app.services.analytics_refresh import mark_products
    mark_products(session, [product_id])
    return saved


def images_table_available(session):
    return inspect(session.connection()).has_table(ProductImage.__tablename__)


def cover_images(session, product_ids):
    """Return one current official cover per product with one batched query."""
    if not product_ids or not images_table_available(session):
        return {}
    ranked = select(ProductImage.product_id, ProductImage.image_url, ProductImage.thumbnail_url,
                    ProductImage.observed_at,
                    func.row_number().over(partition_by=ProductImage.product_id,
                                           order_by=(case((ProductImage.source_kind == "detail", 0), else_=1),
                                                     ProductImage.position, ProductImage.id)).label("rank")) \
        .where(ProductImage.product_id.in_(product_ids), ProductImage.is_current.is_(True)).subquery()
    return {row.product_id: {"image_url": row.image_url, "thumbnail_url": row.thumbnail_url,
                             "image_observed_at": row.observed_at}
            for row in session.execute(select(ranked).where(ranked.c.rank == 1))}
