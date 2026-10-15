import asyncio
from types import SimpleNamespace
from uuid import uuid4

from app.models import TaskStatus, TaskType
from app.queue.contracts import TaskMessage
from app.queue.retry_policy import Failure, TaskFailure
from app.services.task_runtime import ExecutionResult, LeaseLost, claim_task, finalize_task, renew_lease
from app.settings import Settings
from app.workers.reviews import AsyncReviewWorker


class Gate:
    async def pause_remaining(self):
        return 0


def message():
    return TaskMessage(str(uuid4()), TaskType.PRODUCT_REVIEWS)


def worker(handler, *, renew_error=False):
    settings = Settings(enabled_task_types=(TaskType.PRODUCT_REVIEWS,), review_concurrency=2,
                        lease_seconds=3, heartbeat_seconds=1)
    w = AsyncReviewWorker(settings, None, Gate(), "test", handler)
    w.commits, w.renewals = [], 0
    async def transaction(fn, *args, **kwargs):
        if fn is claim_task:
            return SimpleNamespace(task_id=args[0], task_type=args[1], attempt_no=1)
        if fn is renew_lease:
            w.renewals += 1
            if renew_error:
                raise LeaseLost()
        if fn is finalize_task:
            w.commits.append(kwargs)
            return TaskStatus.RETRY_WAITING if "failure" in kwargs else TaskStatus.SUCCEEDED
    w.transaction = transaction
    return w


def test_concurrency_is_real_and_bounded():
    async def run():
        class Handler:
            active = peak = 0
            async def prepare(self, *args):
                self.active += 1
                self.peak = max(self.peak, self.active)
                await asyncio.sleep(.02)
                self.active -= 1
                return ExecutionResult()
        handler = Handler()
        w = worker(handler)
        result = await asyncio.gather(*(w.process(message()) for _ in range(6)))
        assert result == ["ack"] * 6 and handler.peak == 2 and len(w.commits) == 6
    asyncio.run(run())


def test_network_wait_keeps_renewing_lease_and_lost_lease_cancels_work():
    async def run():
        class Handler:
            cancelled = False
            async def prepare(self, *args):
                try:
                    await asyncio.sleep(30)
                finally:
                    self.cancelled = True
        handler = Handler()
        w = worker(handler, renew_error=True)
        assert await asyncio.wait_for(w.process(message()), timeout=3) == "ack"
        assert w.renewals == 1 and not w.commits and handler.cancelled
    asyncio.run(run())


def test_cancellation_does_not_commit_partial_http_work():
    async def run():
        started, cancelled = asyncio.Event(), asyncio.Event()
        class Handler:
            async def prepare(self, *args):
                started.set()
                try:
                    await asyncio.sleep(30)
                finally:
                    cancelled.set()
        w = worker(Handler())
        task = asyncio.create_task(w.process(message()))
        await started.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert cancelled.is_set() and not w.commits
    asyncio.run(run())


def test_domain_failure_is_recorded_and_never_committed_as_empty_success():
    async def run():
        class Handler:
            async def prepare(self, *args):
                raise TaskFailure(Failure("review_api_business_error", "error", True))
        w = worker(Handler())
        assert await w.process(message()) == "ack"
        assert len(w.commits) == 1 and w.commits[0]["failure"].code == "review_api_business_error"
    asyncio.run(run())
