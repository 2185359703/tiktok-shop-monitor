"""Read-only recommendations and token-protected human matching decisions."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.routes import require_write_token
from app.models.matching import MatchStatus
from app.services.matching_service import (
    MatchingError, candidate_matches, confirm_match, list_matches,
    matching_summary, review_match, serialize_match,
)
from app.storage.database import session_scope


router = APIRouter(prefix="/api/v1", tags=["matching"])
Identifier = Annotated[int, Field(ge=1, le=18446744073709551615)]
PathIdentifier = Annotated[int, Path(ge=1, le=18446744073709551615)]


def report_error(error):
    message = {
        "sku_specs_not_exact_or_incomplete": "双方SKU的颜色、原始尺码和完整规格必须齐全且一致。缺失属性或不同尺码体系不能默认相同。",
        "variant_does_not_belong_to_product": "选择的SKU不属于对应商品。",
        "product_not_found_in_market": "商品不存在，或不属于当前监测市场。",
        "cannot_match_product_to_itself": "请选取两件不同商品。",
        "product_match_must_be_confirmed": "请先确认商品同款关系，再确认精确SKU映射。",
    }.get(error.code, "匹配请求未通过校验。")
    raise HTTPException(error.status, {"code": error.code, "message": message, "reasons": error.reasons}) from None


@router.get("/matches")
def matches(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 20,
            after_id: Annotated[int, Query(ge=0)] = 0,
            product_id: Annotated[int | None, Query(ge=1)] = None, status: MatchStatus | None = None):
    with request.app.state.factory() as session:
        try:
            return list_matches(session, request.app.state.settings, limit=limit, after_id=after_id, product_id=product_id, status=status)
        except MatchingError as error:
            report_error(error)


@router.get("/matches/candidates")
def candidates(request: Request, product_id: Annotated[int | None, Query(ge=1)] = None,
               limit: Annotated[int, Query(ge=1, le=20)] = 20):
    with request.app.state.factory() as session:
        try:
            return candidate_matches(session, request.app.state.settings, product_id=product_id, limit=limit)
        except MatchingError as error:
            report_error(error)


@router.get("/matches/summary")
def summary(request: Request):
    with request.app.state.factory() as session:
        return matching_summary(session, request.app.state.settings)


@router.get("/products/{product_id}/matches")
def product_matches(product_id: PathIdentifier, request: Request,
                    limit: Annotated[int, Query(ge=1, le=100)] = 20,
                    after_id: Annotated[int, Query(ge=0)] = 0, status: MatchStatus | None = None):
    with request.app.state.factory() as session:
        try:
            return list_matches(session, request.app.state.settings, product_id=product_id, limit=limit, after_id=after_id, status=status)
        except MatchingError as error:
            report_error(error)


class ConfirmMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left_product_id: Identifier
    right_product_id: Identifier
    match_type: Literal["same_product", "same_sku"] = "same_product"
    left_variant_id: Identifier | None = None
    right_variant_id: Identifier | None = None
    confirmed: Literal[True]
    note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_variants(self):
        if self.left_product_id == self.right_product_id:
            raise ValueError("Two different products are required.")
        if self.match_type == "same_sku" and (self.left_variant_id is None or self.right_variant_id is None):
            raise ValueError("Both variant IDs are required for an exact SKU mapping.")
        if self.match_type == "same_product" and (self.left_variant_id is not None or self.right_variant_id is not None):
            raise ValueError("Product relations do not accept variant IDs.")
        return self


class ReviewMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: MatchStatus
    note: str | None = Field(default=None, max_length=1000)


@router.post("/matches", dependencies=[Depends(require_write_token)])
def create_match(body: ConfirmMatch, request: Request):
    with session_scope(request.app.state.factory) as session:
        try:
            match, sku, created = confirm_match(session, request.app.state.settings, **body.model_dump(exclude={"confirmed"}))
            return {"item": serialize_match(session, request.app.state.settings, match), "created": created,
                    "sku_match_id": str(sku.id) if sku is not None else None}
        except MatchingError as error:
            report_error(error)


@router.patch("/matches/{match_id}", dependencies=[Depends(require_write_token)])
def update_match(match_id: PathIdentifier, body: ReviewMatch, request: Request):
    with session_scope(request.app.state.factory) as session:
        try:
            match = review_match(session, request.app.state.settings, match_id, body.status, note=body.note)
            return {"item": serialize_match(session, request.app.state.settings, match)}
        except MatchingError as error:
            report_error(error)


@router.patch("/matches/{match_id}/skus/{sku_match_id}", dependencies=[Depends(require_write_token)])
def update_sku_match(match_id: PathIdentifier, sku_match_id: PathIdentifier, body: ReviewMatch, request: Request):
    with session_scope(request.app.state.factory) as session:
        try:
            match = review_match(session, request.app.state.settings, match_id, body.status, sku_match_id=sku_match_id, note=body.note)
            return {"item": serialize_match(session, request.app.state.settings, match)}
        except MatchingError as error:
            report_error(error)
