"""Account for a fixed seed scope; queue exhaustion alone is not full coverage."""

from collections import Counter, defaultdict

from sqlalchemy import and_, case, func, select

from app.models import CrawlTask, Product, ProductSnapshot, ProductVariant, ResourceType, SeedShopObservation, Shop, ShopSeedLink, TaskStatus, TaskType
from app.services.image_backfill import BACKFILL_MODES, capture_mode_expression
from app.services.shop_profile_reporting import shop_profile_coverage


ACTIVE = {TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRY_WAITING, TaskStatus.ABANDONED}


def owned_task_queries(seed_ids, shop_ids):
    """Keep disjoint scopes separate so MySQL can use the resource index.

    An OR containing an IN(CAST(products.id AS CHAR)) subquery can turn into
    a dependent subquery for every task. The explicit product join retains
    exact string identity without repeatedly scanning the product table.
    No task payload, checkpoint, or other large JSON column is selected.
    """
    columns = (CrawlTask.task_type, CrawlTask.status, CrawlTask.last_error)
    market = (CrawlTask.platform == "tiktok_shop", CrawlTask.region == "US")
    return (
        select(*columns).where(*market, CrawlTask.task_type == TaskType.SHOP_DISCOVERY,
                               CrawlTask.resource_id.in_([str(value) for value in seed_ids])),
        select(*columns).where(*market, CrawlTask.task_type == TaskType.PRODUCT_DISCOVERY,
                               CrawlTask.resource_id.in_([str(value) for value in shop_ids])),
        select(*columns).select_from(Product).join(CrawlTask, and_(
            CrawlTask.resource_type == ResourceType.PRODUCT,
            CrawlTask.resource_id == func.cast(Product.id, CrawlTask.resource_id.type),
            *market,
        )).where(Product.shop_id.in_(shop_ids), capture_mode_expression().not_in(BACKFILL_MODES), CrawlTask.task_type.in_((
            TaskType.PRODUCT_DETAIL, TaskType.PRODUCT_SNAPSHOT, TaskType.CHANGE_DETECTION))),
    )


def collection_progress(session, batch, *, require_baseline=True, profile_interval_seconds=86400):
    hashes = [row.row_hash for row in batch.rows]
    seeds = session.scalars(select(SeedShopObservation).where(SeedShopObservation.row_hash.in_(hashes))).all()
    seed_ids = [seed.id for seed in seeds]
    links = dict(session.execute(select(ShopSeedLink.seed_id, ShopSeedLink.shop_id).where(
        ShopSeedLink.seed_id.in_(seed_ids))).all())
    shop_ids = list(set(links.values()))
    shops = {shop.id: shop for shop in session.scalars(select(Shop).where(Shop.id.in_(shop_ids)))}
    product_ids = select(Product.id).where(Product.shop_id.in_(shop_ids))
    scopes = owned_task_queries(seed_ids, shop_ids)
    count_groups, error_groups = Counter(), Counter()
    for query in scopes:
        grouped = query.with_only_columns(CrawlTask.task_type, CrawlTask.status, func.count())\
            .group_by(CrawlTask.task_type, CrawlTask.status)
        for kind, status, count in session.execute(grouped):
            count_groups[kind, status] += count
        dead = query.with_only_columns(CrawlTask.task_type, CrawlTask.last_error, func.count())\
            .where(CrawlTask.status == TaskStatus.DEAD).group_by(CrawlTask.task_type, CrawlTask.last_error)
        for kind, message, count in session.execute(dead):
            error_groups[kind, message] += count
    counts = [(kind, status, count) for (kind, status), count in count_groups.items()]
    active = sum(count for _, status, count in counts if status in ACTIVE)
    failed = sum(count for _, status, count in counts if status in {TaskStatus.DEAD, TaskStatus.CANCELLED})
    pages = session.execute(select(CrawlTask.resource_id, CrawlTask.status,
        CrawlTask.checkpoint["complete"].as_boolean(), CrawlTask.checkpoint["stop_reason"].as_string()).where(
        CrawlTask.task_type == TaskType.PRODUCT_DISCOVERY, CrawlTask.platform == "tiktok_shop", CrawlTask.region == "US",
        CrawlTask.resource_id.in_([str(value) for value in shop_ids]))).all()
    page_states = defaultdict(Counter)
    identities = {int(resource): status for resource, status in session.execute(select(CrawlTask.resource_id, CrawlTask.status).where(
        CrawlTask.task_type == TaskType.SHOP_DISCOVERY, CrawlTask.platform == "tiktok_shop", CrawlTask.region == "US",
        CrawlTask.resource_id.in_([str(value) for value in seed_ids])))}
    for shop_id, status, complete, reason in pages:
        page_states[int(shop_id)][status.value] += 1
        if status == TaskStatus.SUCCEEDED and complete and reason == "source_exhausted":
            page_states[int(shop_id)]["source_exhausted"] += 1
        if reason == "page_budget": page_states[int(shop_id)]["page_budget"] += 1
    product_counts = {shop_id: (count, completed) for shop_id, count, completed in session.execute(
        select(Product.shop_id, func.count(), func.sum(case((Product.last_success_at.is_not(None), 1), else_=0)))
        .where(Product.shop_id.in_(shop_ids)).group_by(Product.shop_id))}
    baseline_products = session.scalar(select(func.count(func.distinct(ProductSnapshot.product_id))).where(
        ProductSnapshot.product_id.in_(product_ids)))
    variants = session.scalar(select(func.count()).select_from(ProductVariant).where(ProductVariant.product_id.in_(product_ids)))
    details = sum(int(value[1]) for value in product_counts.values())
    products = sum(value[0] for value in product_counts.values())
    stores = []
    for seed in sorted(seeds, key=lambda row: (row.category, row.seed_value)):
        shop = shops.get(links.get(seed.id))
        pages_for_shop = page_states[shop.id] if shop is not None else Counter()
        state = "identity_pending" if shop is None else "collecting"
        if shop is None and seed.resolution_status.value != "pending": state = "identity_unresolved"
        if shop is None and identities.get(seed.id) in {TaskStatus.DEAD, TaskStatus.CANCELLED}: state = "identity_failed"
        if pages_for_shop["source_exhausted"]: state = "source_complete"
        elif pages_for_shop["page_budget"]: state = "page_budget"
        elif pages_for_shop["dead"] or pages_for_shop["cancelled"]: state = "discovery_failed"
        count, completed = product_counts.get(shop.id, (0, 0)) if shop is not None else (0, 0)
        stores.append({"seed_name": seed.seed_value, "name": shop.canonical_name if shop is not None else seed.seed_value,
                       "category": seed.category, "shop_url": shop.canonical_url if shop is not None else seed.shop_url,
                       "platform_shop_id": shop.platform_shop_id if shop is not None else None,
                       "source_status": state, "products": count, "details_completed": int(completed),
                       "pages_succeeded": pages_for_shop["succeeded"]})
    complete = sum(row["source_status"] == "source_complete" for row in stores)
    errors = [(kind, message, count) for (kind, message), count in error_groups.items()]
    profiles = shop_profile_coverage(session, shops.values(), interval_seconds=profile_interval_seconds)
    profiles["unresolved_seed_observations"] = len(batch.rows) - len(links)
    profiles["full_coverage"] = profiles["full_coverage"] and profiles["unresolved_seed_observations"] == 0
    return {"target_shops": len(batch.rows), "seed_observations": len(seeds), "resolved_shops": len(shop_ids),
            "source_complete_shops": complete, "products": products, "details_completed": details,
            "variants": variants, "baseline_products": baseline_products, "active_tasks": active, "failed_tasks": failed,
            "full_coverage": len(seeds) == len(batch.rows) == complete and details == products and failed == 0 and active == 0
                             and (not require_baseline or baseline_products == details),
            "task_counts": [{"task_type": kind.value, "status": status.value, "count": count} for kind, status, count in counts],
            "failures": [{"task_type": kind.value, "message": message, "count": count} for kind, message, count in errors],
            "shop_profiles": profiles, "shops": stores}
