"""Read independently stored public merchant descriptions without page requests."""

from fastapi import HTTPException, Request
from sqlalchemy import select

from app.api.routes import Db, scope
from app.models import Product, ProductContent
from app.services.product_content import product_content_table_available


from app.api.async_router import AsyncAPIRouter


router = AsyncAPIRouter(prefix="/api/v1")


@router.get("/products/{product_id}/content")
def product_content(product_id: int, request: Request, db: Db):
    product = db.scalar(select(Product).where(Product.id == product_id, *scope(Product, request)))
    if product is None:
        raise HTTPException(404, "product_not_found")
    if not product_content_table_available(db):
        return {"product_id": str(product_id), "readiness": False, "available": False,
                "status": "pending_schema", "content": None}
    row = db.scalar(select(ProductContent).where(ProductContent.product_id == product_id))
    if row is None:
        return {"product_id": str(product_id), "readiness": True, "available": False,
                "status": "not_captured", "content": None}
    return {"product_id": str(product_id), "readiness": True,
            "available": bool(row.sections.get("description") or row.sections.get("specifications")),
            "status": row.completeness,
            "content": {"description_text": row.description_text, "sections": row.sections,
                        "completeness": row.completeness, "source_version": row.source_version,
                        "observed_at": row.observed_at, "unsupported_blocks": row.unsupported_blocks,
                        "source_shape": row.source_shape, "source_url": row.source_url,
                        "content_hash": row.content_hash, "content_version": row.content_version}}

