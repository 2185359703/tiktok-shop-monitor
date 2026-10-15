"""Verify observed public storefront URLs before enriching the seed list."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crawlers.public_shop import PublicPageError, PublicPageSnapshot, ShopCandidate, public_url, shop_id_from_url
from app.models import CrawlTask, ResolutionStatus, SeedShopObservation, Shop, ShopSeedLink, TaskStatus, TaskType
from app.services.seed_importer import SeedRow
from app.services.shop_discovery import normalized_name, resolve_seed_from_snapshot


@dataclass(frozen=True)
class VerifiedStoreLink:
    shop: ShopCandidate
    shop_url: str
    matched_by: str


def verify_store_link(
    row: SeedRow, snapshot: PublicPageSnapshot, *, expected_id: str | None = None,
) -> VerifiedStoreLink:
    """The storefront path, page ID and observed name must agree."""
    identifier = shop_id_from_url(snapshot.final_url)
    if not identifier:
        raise PublicPageError("not_a_storefront", "链接不是可识别的公开店铺入口。")
    if expected_id is not None and identifier != expected_id:
        raise PublicPageError("identity_conflict", "公开链接与已确认店铺ID不一致。")
    primary = [shop for shop in snapshot.candidates if shop.platform_shop_id == identifier]
    if len(primary) != 1 or primary[0].region != row.normalized["region"]:
        raise PublicPageError("unconfirmed_storefront", "页面未提供与入口一致的美国站店铺身份。")
    shop = primary[0]
    if expected_id is None and not any(
        normalized_name(name) == normalized_name(str(row.normalized["seed_value"]))
        for name in shop.observed_names
    ):
        raise PublicPageError("name_mismatch", "实际店铺名称与种子不一致，不能仅凭搜索结果确认。")
    return VerifiedStoreLink(shop, public_url(snapshot.final_url), "stable_id" if expected_id else "exact_name")


def sync_verified_store_link(
    session: Session, row: SeedRow, snapshot: PublicPageSnapshot, evidence_path: str,
) -> str:
    """Enrich confirmed identities without rewriting the original observation/hash."""
    seed = session.scalar(select(SeedShopObservation).where(
        SeedShopObservation.row_hash == row.row_hash
    ).with_for_update())
    if seed is None:
        return "original_observation_missing"
    link = session.scalar(select(ShopSeedLink).where(ShopSeedLink.seed_id == seed.id).with_for_update())
    shop = session.scalar(select(Shop).where(Shop.id == link.shop_id).with_for_update()) if link else None
    checked = verify_store_link(row, snapshot, expected_id=shop.platform_shop_id if shop else None)
    if seed.resolution_status == ResolutionStatus.RESOLVED and shop:
        if shop.last_seen_at <= snapshot.captured_at:
            shop.canonical_url = checked.shop_url
            shop.last_seen_at = snapshot.captured_at
        outcome = "url_enriched"
    else:
        outcome, _ = resolve_seed_from_snapshot(
            session, seed.id, snapshot, evidence_path, verified_store_entry=True,
        )
        if outcome != "resolved":
            return outcome
        link = session.scalar(select(ShopSeedLink).where(ShopSeedLink.seed_id == seed.id))
        shop = session.get(Shop, link.shop_id)
        shop.canonical_url = checked.shop_url
    # Only queued tasks may have their missing entrance supplemented. Never
    # touch execution status, checkpoints, leases or a running worker's payload.
    tasks = session.scalars(select(CrawlTask).where(
        CrawlTask.platform == shop.platform, CrawlTask.region == shop.region,
        CrawlTask.task_type == TaskType.PRODUCT_DISCOVERY, CrawlTask.resource_id == str(shop.id),
        CrawlTask.status == TaskStatus.PENDING, CrawlTask.lease_token.is_(None),
    ).with_for_update()).all()
    for task in tasks:
        task.payload = {**task.payload, "shop_url": shop.canonical_url}
    session.flush()
    return outcome
