"""Cancellation, duplicate delivery, and source completion on real InnoDB."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select

from app.models import CollectionLaunch, CollectionLaunchTask, CrawlAttempt, CrawlTask, TaskStatus, TaskType
from app.services import collection_launch_control as control
from app.services.collection_intake import create_launch, inherit_launch_tasks
from app.services.product_discovery import persist_product_page
from app.services.task_runtime import ExecutionResult, claim_task, database_now, finalize_task
from test_collection_intake import CONFIG, definition, roots, store
from test_collection_launch_concurrency import active_count, isolated_factory
from test_product_list import snapshot


def shared_seed_fixture(db, *, owners=2):
    body = definition(kind=TaskType.SHOP_DISCOVERY,
        urls=["https://shop.tiktok.com/us/store/cancellation-fixture/7490000000000000888"])
    launches = [create_launch(db, CONFIG, body, uuid4())[0] for _ in range(owners)]
    task = roots(db, launches[0])[0]
    assert all(roots(db, launch)[0].id == task.id for launch in launches)
    return launches, task


@pytest.mark.parametrize("owners", [1, 2])
def test_duplicate_claims_after_shared_launch_gate_create_one_lease(isolated_factory, monkeypatch, owners):
    factory = isolated_factory
    with factory.begin() as db:
        _, task = shared_seed_fixture(db, owners=owners)
        task_uuid, kind = task.task_id, task.task_type
    gate_acquired = Barrier(2)
    original_gate = control.lock_launch_scope

    def synchronize_after_gate(db, settings, task_id):
        allowed = original_gate(db, settings, task_id)
        assert allowed
        gate_acquired.wait(timeout=8)
        return allowed

    monkeypatch.setattr(control, "lock_launch_scope", synchronize_after_gate)

    def claim(worker):
        with factory.begin() as db:
            return claim_task(db, task_uuid, kind, worker, CONFIG)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim, worker) for worker in ("duplicate-a", "duplicate-b")]
        leases = [future.result(timeout=15) for future in futures]
    assert sum(lease is not None for lease in leases) == 1
    with factory() as db:
        task = db.scalar(select(CrawlTask).where(CrawlTask.task_id == task_uuid))
        assert task.status == TaskStatus.RUNNING and task.attempt_count == 1
        assert db.scalar(select(func.count()).select_from(CrawlAttempt)) == 1


@pytest.mark.parametrize("cleanup", ["claim", "sweep"])
def test_cancel_yields_to_running_completion_and_blocks_late_page_children(isolated_factory, monkeypatch, cleanup):
    factory = isolated_factory
    with factory.begin() as db:
        shop = store(db)
        launch, _ = create_launch(db, CONFIG, definition([shop.id], pages=2), uuid4())
        parent = roots(db, launch)[0]
        launch_id, parent_id = launch.id, parent.id
        lease = claim_task(db, parent.task_id, parent.task_type, "completing-source", CONFIG)
    task_locked, launch_locked, membership_attempted = Event(), Event(), Event()
    original_finish = control.finish_cancellation
    engine = factory.kw["bind"]

    def hold_cancellation_until_completion_needs_membership(db, settings, row, **kwargs):
        if db.info.get("test_role") == "canceller":
            launch_locked.set()
            assert membership_attempted.wait(8), "Source did not reach the inherited membership write."
        return original_finish(db, settings, row, **kwargs)

    def observe_membership_write(connection, cursor, statement, parameters, context, executemany):
        if connection.info.get("test_role") == "source" and statement.startswith("INSERT INTO collection_launch_tasks"):
            membership_attempted.set()

    monkeypatch.setattr(control, "finish_cancellation", hold_cancellation_until_completion_needs_membership)

    def complete():
        with factory.begin() as db:
            db.connection().info["test_role"] = "source"
            def apply(session, task):
                task_locked.set()
                assert launch_locked.wait(8), "Cancellation did not acquire the launch lock."
                persist_product_page(session, task, snapshot(), page_number=1,
                    root_task_id=task.task_id, max_pages=2)
            return finalize_task(db, lease, CONFIG, result=ExecutionResult(apply=apply))

    def cancel():
        assert task_locked.wait(8), "Completion did not acquire its source task lock."
        with factory.begin() as db:
            db.info["test_role"] = "canceller"
            db.connection().info["test_role"] = "canceller"
            return control.cancel_launch(db, CONFIG, launch_id)

    event.listen(engine, "before_cursor_execute", observe_membership_write)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            completed, cancelled = pool.submit(complete), pool.submit(cancel)
            result = cancelled.result(timeout=15)
            assert result["changed"] and result["still_running_count"] == 1
            assert result["launch"]["status"] == "cancelling"
            assert completed.result(timeout=15) == TaskStatus.SUCCEEDED
    finally:
        event.remove(engine, "before_cursor_execute", observe_membership_write)

    with factory.begin() as db:
        assert db.get(CrawlTask, parent_id).status == TaskStatus.SUCCEEDED
        children = db.scalars(select(CrawlTask).join(CollectionLaunchTask, CollectionLaunchTask.task_id == CrawlTask.id)
            .where(CollectionLaunchTask.launch_id == launch_id, CollectionLaunchTask.member_role == "descendant")).all()
        assert len(children) == 2 and all(child.status == TaskStatus.PENDING for child in children)
        if cleanup == "claim":
            for child in children:
                assert claim_task(db, child.task_id, child.task_type, "late-delivery", CONFIG) is None
            assert control.sweep_cancelled_launches(db, CONFIG) == 0
        else:
            assert control.sweep_cancelled_launches(db, CONFIG) == 2
        db.flush()
        db.expire_all()
        assert all(child.status == TaskStatus.CANCELLED and child.attempt_count == 0 for child in children)
        assert db.get(CollectionLaunch, launch_id).cancel_completed_at is not None
        assert db.scalar(select(func.count()).select_from(CrawlAttempt)) == 1


def test_shared_owner_cancellation_during_claim_retains_live_work_and_descendants(isolated_factory, monkeypatch):
    factory = isolated_factory
    with factory.begin() as db:
        launches, task = shared_seed_fixture(db)
        first_id, second_id = [launch.id for launch in launches]
        task_uuid, kind = task.task_id, task.task_type
    launch_locked, claimant_at_gate = Event(), Event()
    original_finish = control.finish_cancellation
    original_gate = control.lock_launch_scope

    def cancellation_held(db, settings, launch, **kwargs):
        launch_locked.set()
        assert claimant_at_gate.wait(8), "Claim did not enter the owner gate."
        return original_finish(db, settings, launch, **kwargs)

    def gate_with_signal(db, settings, task_id):
        claimant_at_gate.set()
        return original_gate(db, settings, task_id)

    monkeypatch.setattr(control, "finish_cancellation", cancellation_held)
    monkeypatch.setattr(control, "lock_launch_scope", gate_with_signal)

    def cancel():
        with factory.begin() as db:
            return control.cancel_launch(db, CONFIG, first_id)

    def claim():
        assert launch_locked.wait(8), "Cancellation did not hold its launch."
        with factory.begin() as db:
            return claim_task(db, task_uuid, kind, "shared-live-owner", CONFIG)

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancellation, claiming = pool.submit(cancel), pool.submit(claim)
        result = cancellation.result(timeout=15)
        lease = claiming.result(timeout=15)
    assert result["shared_task_count"] == 1 and result["cancelled_task_count"] == 0
    assert lease is not None
    with factory.begin() as db:
        assert db.get(CollectionLaunch, first_id).cancel_completed_at is None
        assert db.get(CollectionLaunch, first_id).cancel_pending
        assert db.get(CollectionLaunch, second_id).cancel_requested_at is None
        children = []
        def apply(session, parent):
            child = CrawlTask(platform=parent.platform, region=parent.region, task_type=TaskType.CHANGE_DETECTION,
                resource_type=parent.resource_type, resource_id=parent.resource_id, payload={},
                scheduled_for=parent.scheduled_for, available_at=database_now(session))
            session.add(child)
            session.flush()
            inherit_launch_tasks(session, parent, [child])
            children.append(child)
        assert finalize_task(db, lease, CONFIG, result=ExecutionResult(apply=apply)) == TaskStatus.SUCCEEDED
        child = children[0]
        assert set(db.scalars(select(CollectionLaunchTask.launch_id).where(CollectionLaunchTask.task_id == child.id))) == {first_id, second_id}
        child_lease = claim_task(db, child.task_id, child.task_type, "live-owner-descendant", CONFIG)
        assert child_lease is not None
        assert db.scalar(select(func.count()).select_from(CrawlAttempt)) == 2
        assert finalize_task(db, child_lease, CONFIG, result=ExecutionResult()) == TaskStatus.SUCCEEDED
        assert control.sweep_cancelled_launches(db, CONFIG) == 0
        assert db.get(CollectionLaunch, first_id).cancel_completed_at is not None
        assert not db.get(CollectionLaunch, first_id).cancel_pending


def test_simultaneous_cancellation_of_all_shared_owners_leaves_no_unsweepable_tasks(isolated_factory, monkeypatch):
    factory = isolated_factory
    with factory.begin() as db:
        launches, _ = shared_seed_fixture(db)
        launch_ids = [launch.id for launch in launches]
    both_marked, both_finished = Barrier(2), Barrier(2)
    original_finish = control.finish_cancellation

    def finish_after_both_cancel_flags(db, settings, launch, **kwargs):
        both_marked.wait(timeout=8)
        return original_finish(db, settings, launch, **kwargs)
    monkeypatch.setattr(control, "finish_cancellation", finish_after_both_cancel_flags)

    def cancel(launch_id):
        with factory.begin() as db:
            result = control.cancel_launch(db, CONFIG, launch_id)
            both_finished.wait(timeout=8)
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(cancel, launch_id) for launch_id in launch_ids]
        assert all(future.result(timeout=15)["changed"] for future in futures)
    monkeypatch.setattr(control, "finish_cancellation", original_finish)
    with factory.begin() as db:
        assert all(db.get(CollectionLaunch, launch_id).cancel_pending for launch_id in launch_ids)
        assert all(db.get(CollectionLaunch, launch_id).cancel_completed_at is None for launch_id in launch_ids)
        control.sweep_cancelled_launches(db, CONFIG)
        assert active_count(db) == 0
        assert all(db.get(CollectionLaunch, launch_id).cancel_completed_at is not None for launch_id in launch_ids)
        assert all(not db.get(CollectionLaunch, launch_id).cancel_pending for launch_id in launch_ids)


def test_sweep_rotates_past_ten_shared_active_batches_to_cancel_later_work(isolated_factory):
    factory = isolated_factory
    with factory.begin() as db:
        owners, shared_task = shared_seed_fixture(db, owners=12)
        shared_id = shared_task.id
        for owner in owners[:-1]:
            result = control.cancel_launch(db, CONFIG, owner.id)
            assert result["shared_task_count"] == 1 and owner.cancel_pending
        launch, _ = create_launch(db, CONFIG, definition([store(db).id]), uuid4())
        root = roots(db, launch)[0]
        stamp = database_now(db)
        children = [CrawlTask(platform=root.platform, region=root.region, task_type=TaskType.CHANGE_DETECTION,
            resource_type=root.resource_type, resource_id=root.resource_id, payload={},
            scheduled_for=root.scheduled_for, available_at=stamp) for _ in range(100)]
        db.add_all(children)
        db.flush()
        inherit_launch_tasks(db, root, children)
        result = control.cancel_launch(db, CONFIG, launch.id)
        assert result["cancelled_task_count"] == 100 and result["pending_remaining_count"] == 1
        tail_id = launch.id
    with factory.begin() as db:
        assert control.sweep_cancelled_launches(db, CONFIG, limit=1) == 0
        assert db.get(CollectionLaunch, tail_id).cancel_pending
        assert active_count(db) == 2
    with factory.begin() as db:
        assert control.sweep_cancelled_launches(db, CONFIG, limit=1) == 1
        assert db.get(CollectionLaunch, tail_id).cancel_completed_at is not None
        assert not db.get(CollectionLaunch, tail_id).cancel_pending
        assert db.get(CrawlTask, shared_id).status == TaskStatus.PENDING
        assert active_count(db) == 1
