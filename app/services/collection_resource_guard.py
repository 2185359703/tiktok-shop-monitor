"""Serialize explicit launches and retries against the same source resource."""

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.models import CrawlTask, Product, ResourceType, Shop, TaskStatus, TaskType


GUARDED_TYPES = frozenset({TaskType.PRODUCT_DISCOVERY, TaskType.PRODUCT_DETAIL, TaskType.PRODUCT_REVIEWS})
ACTIVE_STATES = (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRY_WAITING, TaskStatus.ABANDONED)


class CollectionResourceBusy(ValueError):
    """A concurrent owner or active task prevents admitting more source work."""


def lock_idle_resources(session, settings, kind, identifiers):
    """Keep resource locks through task insertion in the caller's transaction.

    Locking reads see current committed work even after an earlier repeatable
    read snapshot. Worker completion owns its task before its resource, so the
    subsequent task read must never wait while holding a resource lock.
    """
    if kind not in GUARDED_TYPES:
        raise ValueError("unsupported_collection_resource_type")
    model, resource_type = (Shop, ResourceType.SHOP) if kind == TaskType.PRODUCT_DISCOVERY else (Product, ResourceType.PRODUCT)
    identifiers = sorted({int(value) for value in identifiers})
    resources = session.scalars(select(model).where(model.platform == settings.platform, model.region == settings.region,
        model.id.in_(identifiers)).order_by(model.id).with_for_update().execution_options(populate_existing=True)).all()
    try:
        for resource in resources:
            query = select(CrawlTask.id).where(CrawlTask.platform == settings.platform, CrawlTask.region == settings.region,
                CrawlTask.task_type == kind, CrawlTask.resource_type == resource_type, CrawlTask.resource_id == str(resource.id),
                CrawlTask.status.in_(ACTIVE_STATES)).with_hint(CrawlTask, "FORCE INDEX (ix_crawl_tasks_resource)", dialect_name="mysql")
            if session.scalar(query.limit(1).with_for_update(nowait=True)) is not None:
                raise CollectionResourceBusy("collection_resource_busy")
    except OperationalError as error:
        if getattr(error.orig, "args", (None,))[0] == 3572:
            raise CollectionResourceBusy("collection_resource_busy") from None
        raise
    return resources
