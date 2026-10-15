"""Read public photo galleries; the API never proxies or downloads pictures."""

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import case, select

from app.api.routes import Db, scope
from app.models import Product
from app.models.product_image import ProductImage
from app.services.product_images import images_table_available


router = APIRouter(prefix="/api/v1")


@router.get("/products/{product_id}/images")
def product_images(product_id: int, request: Request, db: Db):
    product = db.scalar(select(Product).where(Product.id == product_id, *scope(Product, request)))
    if product is None:
        raise HTTPException(404, "product_not_found")
    if not images_table_available(db):
        return {"product_id": str(product_id), "status": "pending_schema", "items": [],
                "message": "图片存储尚未启用，现有采集仍在继续。"}
    rows = db.scalars(select(ProductImage).where(ProductImage.product_id == product_id,
                                                  ProductImage.is_current.is_(True))
                      .order_by(case((ProductImage.source_kind == "detail", 0), else_=1),
                                ProductImage.position, ProductImage.id).limit(100)).all()
    return {"product_id": str(product_id), "status": "available" if rows else "not_captured",
            "items": [{"id": str(row.id), "image_url": row.image_url, "thumbnail_url": row.thumbnail_url,
                       "role": row.role, "position": row.position, "source_url": row.source_url,
                       "source_kind": row.source_kind, "source_version": row.source_version,
                       "observed_at": row.observed_at} for row in rows],
            "message": None if rows else "历史采集未保留图片；后续详情采集或显式补采后显示。"}
