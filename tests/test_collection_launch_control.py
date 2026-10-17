from uuid import uuid4

import pytest

from sqlalchemy import func, select

from app.models import CollectionOperation, CrawlAttempt, CrawlTask, TaskStatus, TaskType
from app.services.collection_intake import create_launch, inherit_launch_tasks, launch_summary
from app.services.collection_launch_control import cancel_launch, sweep_cancelled_launches
from app.services.collection_management import ManagementError, retry_task
from app.services.task_runtime import ExecutionResult, claim_task, database_now, finalize_task
from test_collection_intake import CONFIG, definition, product, roots, store


def test_batch_cancellation_preserves_running_work_and_blocks_future_descendants(session):
    shop = store(session)
    products = [product(session, shop), product(session, shop)]
    launch, _ = create_launch(session, CONFIG, definition([p.id for p in products], kind=TaskType.PRODUCT_DETAIL), uuid4())
    first, second = roots(session, launch)
    lease = claim_task(session, first.task_id, first.task_type, "running-fixture", CONFIG)
    response = cancel_launch(session, CONFIG, launch.id)
    assert response["changed"] and response["still_running_count"] == 1 and response["cancelled_task_count"] == 1
    assert launch.cancel_requested_at and not launch.cancel_completed_at
    assert first.status == TaskStatus.RUNNING and second.status == TaskStatus.CANCELLED
    assert claim_task(session, second.task_id, second.task_type, "must-not-start", CONFIG) is None
    children = []
    def finish(db, parent):
        child = CrawlTask(platform=parent.platform, region=parent.region, task_type=TaskType.CHANGE_DETECTION,
            resource_type=parent.resource_type, resource_id=parent.resource_id, payload={}, scheduled_for=parent.scheduled_for,
            available_at=database_now(db))
        db.add(child)
        db.flush()
        inherit_launch_tasks(db, parent, [child])
        children.append(child)
    finalize_task(session, lease, CONFIG, result=ExecutionResult(apply=finish))
    assert first.status == TaskStatus.SUCCEEDED
    child = children[0]
    assert claim_task(session, child.task_id, child.task_type, "must-not-start-descendant", CONFIG) is None
    assert child.status == TaskStatus.CANCELLED and child.attempt_count == 0
    assert sweep_cancelled_launches(session, CONFIG) == 0
    assert launch.cancel_completed_at
    assert launch_summary(session, launch)["status"] == "cancelled"
    assert session.scalar(select(func.count()).select_from(CrawlAttempt)) == 1


def test_cancelling_one_shared_seed_batch_does_not_cancel_another_live_owner(session):
    url = "https://shop.tiktok.com/us/store/shared-fixture/7490000000000000999"
    body = definition(kind=TaskType.SHOP_DISCOVERY, urls=[url])
    first, _ = create_launch(session, CONFIG, body, uuid4())
    second, _ = create_launch(session, CONFIG, body, uuid4())
    task = roots(session, first)[0]
    assert roots(session, second)[0].id == task.id
    response = cancel_launch(session, CONFIG, first.id)
    assert response["shared_task_count"] == 1 and response["cancelled_task_count"] == 0
    assert not first.cancel_completed_at and first.cancel_pending and task.status == TaskStatus.PENDING
    assert claim_task(session, task.task_id, task.task_type, "shared-live-owner", CONFIG) is not None
    assert task.status == TaskStatus.RUNNING
    response = cancel_launch(session, CONFIG, second.id)
    assert response["still_running_count"] == 1 and response["cancelled_task_count"] == 0
    assert task.status == TaskStatus.RUNNING


def test_cancellation_is_idempotent_and_completed_batch_history_is_unchanged(session):
    shop = store(session)
    item = product(session, shop)
    launch, _ = create_launch(session, CONFIG, definition([item.id], kind=TaskType.PRODUCT_DETAIL), uuid4())
    task = roots(session, launch)[0]
    response = cancel_launch(session, CONFIG, launch.id)
    assert response["changed"] and response["cancelled_task_count"] == 1
    response = cancel_launch(session, CONFIG, launch.id)
    assert not response["changed"] and response["cancelled_task_count"] == 0
    assert session.scalar(select(func.count()).select_from(CollectionOperation).where(CollectionOperation.action == "launch_cancelled")) == 1
    with pytest.raises(ManagementError, match="collection_launch_cancelled"):
        retry_task(session, CONFIG, task.task_id)
    second, _ = create_launch(session, CONFIG, definition([item.id], kind=TaskType.PRODUCT_DETAIL), uuid4())
    done = roots(session, second)[0]
    lease = claim_task(session, done.task_id, done.task_type, "done-fixture", CONFIG)
    finalize_task(session, lease, CONFIG, result=ExecutionResult())
    response = cancel_launch(session, CONFIG, second.id)
    assert not response["changed"] and response["launch"]["status"] == "succeeded"
    assert second.cancel_requested_at is None and done.status == TaskStatus.SUCCEEDED


def test_cancellation_cleanup_is_bounded_and_scheduler_finishes_later(session):
    shop = store(session)
    item = product(session, shop)
    launch, _ = create_launch(session, CONFIG, definition([item.id], kind=TaskType.PRODUCT_DETAIL), uuid4())
    parent = roots(session, launch)[0]
    children = []
    for _ in range(130):
        children.append(CrawlTask(platform=parent.platform, region=parent.region, task_type=TaskType.PRODUCT_DETAIL,
            resource_type=parent.resource_type, resource_id=parent.resource_id, payload={}, scheduled_for=parent.scheduled_for,
            available_at=database_now(session)))
    session.add_all(children)
    session.flush()
    inherit_launch_tasks(session, parent, children)
    first = cancel_launch(session, CONFIG, launch.id)
    assert first["cancelled_task_count"] == 100 and first["pending_remaining_count"] == 31
    assert not launch.cancel_completed_at
    assert sweep_cancelled_launches(session, CONFIG, limit=7) == 7
    assert not launch.cancel_completed_at
    assert sweep_cancelled_launches(session, CONFIG) == 24
    assert launch.cancel_completed_at
    assert all(task.status == TaskStatus.CANCELLED for task in [parent, *children])
