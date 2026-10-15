"""Analytics routes read existing observations without triggering collection."""

from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.services.analytics_service import build_workbench, list_analytics_products


router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


def analytics_filters(
    shop_id: Annotated[int | None, Query(ge=1)] = None,
    category: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    currency: Annotated[str | None, Query(pattern="^[A-Za-z]{3}$")] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    price_min: Annotated[Decimal | None, Query(ge=0, le=Decimal("9999999999.99"), max_digits=12, decimal_places=2)] = None,
    price_max: Annotated[Decimal | None, Query(ge=0, le=Decimal("9999999999.99"), max_digits=12, decimal_places=2)] = None,
    stock: Literal["unknown", "in_stock", "out_of_stock", "partial"] | None = None,
    price_upper_exclusive: bool = False,
    price_unknown: bool | None = None,
    attribute: Literal["brand", "gender", "season", "style", "material", "color", "size"] | None = None,
    value: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
):
    if price_min is not None and price_max is not None and price_min > price_max:
        raise HTTPException(422, "price_min_exceeds_price_max")
    if (attribute is None) != (value is None):
        raise HTTPException(422, "attribute_and_value_required_together")
    return {"shop_id": shop_id, "category": category, "currency": currency, "q": q,
            "price_min": price_min, "price_max": price_max, "stock": stock,
            "price_upper_exclusive": price_upper_exclusive, "price_unknown": price_unknown,
            "attribute": attribute, "value": value}


@router.get("/workbench")
def workbench(request: Request, options: Annotated[dict, Depends(analytics_filters)],
              lookback_hours: Annotated[int, Query(ge=1, le=720)] = 24):
    with request.app.state.factory() as db:
        return build_workbench(db, request.app.state.settings, lookback_hours=lookback_hours, **options)


@router.get("/products")
def products(request: Request, options: Annotated[dict, Depends(analytics_filters)],
             page: Annotated[int, Query(ge=1)] = 1,
             page_size: Annotated[int, Query(ge=1, le=100)] = 24,
             sort: Literal["recent", "price_asc", "price_desc", "title", "title_desc", "shop_asc", "shop_desc",
                           "stock_asc", "stock_desc", "sku_count_asc", "sku_count_desc", "rating_desc", "reviews_desc"] = "recent"):
    with request.app.state.factory() as db:
        return list_analytics_products(db, request.app.state.settings, page=page, page_size=page_size, sort=sort, **options)
