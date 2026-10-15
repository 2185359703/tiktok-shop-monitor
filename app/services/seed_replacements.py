"""Retire only unclaimed jobs for observations removed by an approved seed change."""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CrawlTask, ResourceType, SeedShopObservation, ShopSeedLink, TaskStatus, TaskType


def retire_replaced_seed_tasks(
    session: Session, original_row_hash: str, *, active_shop_ids: set[int], replacement_row_hash: str,
) -> int:
    seed = session.scalar(select(SeedShopObservation).where(
        SeedShopObservation.row_hash == original_row_hash
    ).with_for_update())
    if seed is None:
        raise ValueError("找不到待替换的原始观察，不能按名称模糊取消任务。")
    tasks = session.scalars(select(CrawlTask).where(
        CrawlTask.platform == seed.platform, CrawlTask.region == seed.region,
        CrawlTask.task_type == TaskType.SHOP_DISCOVERY,
        CrawlTask.resource_type == ResourceType.SEED, CrawlTask.resource_id == str(seed.id),
    ).with_for_update()).all()
    link = session.scalar(select(ShopSeedLink).where(ShopSeedLink.seed_id == seed.id))
    if link and link.shop_id not in active_shop_ids:
        tasks += session.scalars(select(CrawlTask).where(
            CrawlTask.platform == seed.platform, CrawlTask.region == seed.region,
            CrawlTask.task_type == TaskType.PRODUCT_DISCOVERY,
            CrawlTask.resource_type == ResourceType.SHOP, CrawlTask.resource_id == str(link.shop_id),
        ).with_for_update()).all()
    if any(task.status == TaskStatus.RUNNING or task.lease_token is not None for task in tasks):
        raise ValueError("原店铺有运行中或持有租约的任务，不能自动退休；请先协调执行状态。")
    count = 0
    for task in tasks:
        if task.status not in {TaskStatus.PENDING, TaskStatus.RETRY_WAITING, TaskStatus.ABANDONED}:
            continue
        task.status = TaskStatus.CANCELLED
        task.finished_at = datetime.now(timezone.utc)
        task.checkpoint = {**(task.checkpoint or {}), "retired_by_seed_replacement": {
            "replacement_row_hash": replacement_row_hash, "reason": "user_approved_seed_replacement",
        }}
        count += 1
    session.flush()
    return count
