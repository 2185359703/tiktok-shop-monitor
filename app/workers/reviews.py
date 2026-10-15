"""Coroutine review worker with the existing SQL lease and commit fence."""

from app.queue.async_redis_gate import AsyncRedisGate
from app.workers.async_runtime import AsyncWorker

from app.crawlers.product_reviews import AsyncReviewClient, REVIEW_ENDPOINT
from app.crawlers.public_shop import PublicPageError
from app.models import TaskType
from app.queue.retry_policy import Failure, TaskFailure
from app.services.product_reviews import persist_review_page, validate_review_payload
from app.services.task_runtime import ExecutionResult


ASYNC_TASK_TYPES = frozenset({TaskType.PRODUCT_REVIEWS})


class ReviewHandler:
    async def prepare(self, lease, settings, gate):
        payload = validate_review_payload(lease)
        try:
            async with AsyncReviewClient(before_request=gate.wait_turn, proxy=settings.detail_proxy_url,
                                         backend=settings.review_http_backend) as client:
                snapshot = await client.fetch_page(payload["product_url"], payload["platform_shop_id"],
                    page_number=payload["page_number"], page_size=payload["page_size"])
        except PublicPageError as error:
            if error.code == "access_limited":
                if error.status != 200 or not settings.resource_document_cooldown:
                    await gate.pause()
                raise TaskFailure(Failure(error.code, "评论来源受限，等待冷却后重试。", True,
                    settings.access_pause_seconds, error.status)) from None
            raise TaskFailure(Failure(error.code, "评论请求或来源结构验证失败；已有评论保留。", error.retryable,
                                      response_status=error.status)) from None
        checkpoint = {}

        def apply(session, task):
            checkpoint.update(persist_review_page(session, task, snapshot))

        return ExecutionResult(apply=apply, checkpoint=checkpoint,
            request_url=snapshot.source_url if snapshot.page.source_kind == "document" else REVIEW_ENDPOINT,
            request_method="GET" if snapshot.page.source_kind == "document" else "POST",
            response_status=200, response_bytes=snapshot.response_bytes,
            response_hash=snapshot.response_hash, latency_ms=snapshot.latency_ms)


class AsyncReviewWorker(AsyncWorker):
    def __init__(self, settings, factory, gate, worker_id, handler=None):
        actual = handler or ReviewHandler()
        class Adapter:
            async def prepare(self, lease, context):
                return await actual.prepare(lease, context.settings, context.gate)
        super().__init__(settings, factory, {TaskType.PRODUCT_REVIEWS: Adapter()}, gate, worker_id,
                         concurrency=settings.review_concurrency)
