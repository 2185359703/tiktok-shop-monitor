"""Coroutine handlers; validators and transactional apply rules mirror the legacy adapters."""

import asyncio

from app.crawlers.async_public import AsyncTikTokShopAdapter as TikTokShopAdapter
from app.crawlers.public_shop import PublicPageError, public_url, shop_id_from_url
from app.crawlers.product_list import PublicPagination, save_product_failure
from app.models import TaskType
from app.models import ResourceType
from app.queue.retry_policy import Failure, TaskFailure
from app.services.shop_discovery import resolve_seed_from_snapshot
from app.services.shop_profile import persist_shop_profile
from app.services.product_discovery import persist_product_page
from app.services.task_runtime import ExecutionResult, TaskLease
from app.workers.async_contracts import HandlerContext
from uuid import UUID
from app.crawlers.product_list import identifier, product_id_from_url
from app.crawlers.product_detail import PARSER_VERSION as DETAIL_PARSER_VERSION
from app.crawlers.shop_profile import PARSER_VERSION as PROFILE_PARSER_VERSION
from app.services.product_detail import persist_product_detail
from app.services.product_reviews import ensure_initial_review_task
from app.services.snapshot_service import persist_snapshot
from app.services.change_detector import detect_snapshot_changes
from app.services.async_heavy_slot import heavy_slot
from app.reports.summary import build_monitoring_report
from app.reports.exporter import export_report
from app.crawlers.product_images import IMAGE_SOURCE_VERSION
from app.crawlers.product_content import CONTENT_SOURCE_VERSION
from app.services.image_backfill import BACKFILL_MODES, MEDIA_MODE, image_capture_state, persist_image_observation, save_image_evidence


async def pause_access_scope(context, status):
    if context.settings.resource_document_cooldown and status == 200:
        return
    await context.gate.pause()


def access_message(context, status):
    if context.settings.resource_document_cooldown and status == 200:
        return "当前公开资源返回验证页，任务等待有界冷却重试。"
    return "公开入口受限，已暂停该市场共享请求。"


class ShopDiscoveryHandler:
    task_type = TaskType.SHOP_DISCOVERY

    async def prepare(self, lease: TaskLease, context: HandlerContext) -> ExecutionResult:
        try:
            if lease.payload.get("seed_id") != lease.resource_id or not lease.resource_id.isdecimal():
                raise TaskFailure(Failure("invalid_task_payload", "种子任务的资源标识不一致。"))
            source = public_url(lease.payload.get("shop_url") or lease.payload["source_url"])
            snapshot = await TikTokShopAdapter(context.gate, context.settings).discover_shops(source, context.stop)
        except PublicPageError as error:
            if error.code == "access_limited":
                await pause_access_scope(context, error.status)
                raise TaskFailure(Failure("access_limited", access_message(context, error.status), True,
                                          context.settings.access_pause_seconds, error.status)) from None
            raise TaskFailure(Failure(error.code, "公开来源请求或结构验证失败。", error.retryable,
                                      response_status=error.status)) from None
        path = await asyncio.to_thread(snapshot.save_evidence, context.settings.evidence_directory / lease.task_id)

        def apply(session, task):
            outcome, _ = resolve_seed_from_snapshot(
                session, int(lease.resource_id), snapshot, str(path),
                verified_store_entry=shop_id_from_url(source) is not None,
                runtime_lease_token=lease.lease_token)
            if outcome not in {"resolved", "already_processed"}:
                # Roll back partial identity changes; the runtime records failure.
                raise TaskFailure(Failure(f"identity_{outcome}", "来源未能确认唯一店铺身份，需要补充入口或人工核验。"))

        return ExecutionResult(apply=apply,
                               checkpoint={"source_url": snapshot.final_url, "response_hash": snapshot.response_hash,
                                           "evidence_path": str(path), "outcome": "resolved"},
                               request_url=snapshot.final_url, response_status=snapshot.status,
                               response_bytes=snapshot.response_bytes, response_hash=snapshot.response_hash,
                               latency_ms=snapshot.latency_ms, evidence_path=str(path))


class ShopProfileHandler:
    task_type = TaskType.SHOP_PROFILE

    async def prepare(self, lease: TaskLease, context: HandlerContext) -> ExecutionResult:
        try:
            if lease.payload.get("shop_id") != lease.resource_id or not lease.resource_id.isdecimal():
                raise ValueError("shop_identity")
            source = public_url(lease.payload["shop_url"])
            if shop_id_from_url(source) != lease.payload.get("platform_shop_id"):
                raise ValueError("shop_url")
            snapshot = await TikTokShopAdapter(context.gate, context.settings).fetch_shop_profile(source, context.stop)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise TaskFailure(Failure("invalid_shop_profile_task", "店铺档案任务的资源或来源身份不一致。")) from None
        except PublicPageError as error:
            path = await asyncio.to_thread(save_product_failure, error, context.settings.evidence_directory / lease.task_id, parser_version=PROFILE_PARSER_VERSION)
            diagnostics = getattr(error, "diagnostics", {})
            evidence = {key: diagnostics.get(key) for key in ("request_url", "response_hash", "response_bytes")}
            evidence["evidence_path"] = str(path)
            if error.code == "access_limited":
                await pause_access_scope(context, error.status)
                raise TaskFailure(Failure(error.code, access_message(context, error.status), True,
                                          context.settings.access_pause_seconds, error.status, **evidence)) from None
            raise TaskFailure(Failure(error.code, "公开店铺档案请求或结构验证失败。", error.retryable,
                                      response_status=error.status, **evidence)) from None
        path = await asyncio.to_thread(snapshot.save_evidence, context.settings.evidence_directory / lease.task_id)
        checkpoint = {"source_url": snapshot.final_url, "response_hash": snapshot.response_hash,
                      "evidence_path": str(path), "observation_status": snapshot.profile.observation_status}

        def apply(session, task):
            checkpoint.update(persist_shop_profile(session, task, snapshot, str(path)))

        return ExecutionResult(apply=apply, checkpoint=checkpoint, request_url=snapshot.final_url,
                               response_status=snapshot.status, response_bytes=snapshot.response_bytes,
                               response_hash=snapshot.response_hash, latency_ms=snapshot.latency_ms,
                               evidence_path=str(path))


class ProductDiscoveryHandler:
    task_type = TaskType.PRODUCT_DISCOVERY

    async def prepare(self, lease: TaskLease, context: HandlerContext) -> ExecutionResult:
        try:
            if lease.payload.get("shop_id") != lease.resource_id or not lease.resource_id.isdecimal():
                raise TaskFailure(Failure("invalid_task_payload", "商品发现任务资源标识不一致。"))
            source = public_url(lease.payload["shop_url"])
            if shop_id_from_url(source) != lease.payload.get("platform_shop_id"):
                raise TaskFailure(Failure("invalid_task_payload", "商品任务店铺链接与平台 ID 不一致。"))
            page_number = lease.payload.get("page_number", 1)
            max_pages = lease.payload.get("max_pages", context.settings.product_discovery_max_pages)
            root = lease.payload.get("root_task_id", lease.task_id)
            if (type(page_number) is not int or type(max_pages) is not int or not 1 <= page_number <= max_pages <= 500
                    or str(UUID(root)) != root):
                raise TaskFailure(Failure("invalid_pagination_task", "分页任务窗口或预算不正确。"))
            raw_pagination = lease.payload.get("public_pagination")
            if (page_number == 1) != (raw_pagination is None):
                raise TaskFailure(Failure("invalid_pagination_task", "分页序号与持久化游标不一致。"))
            pagination = PublicPagination.from_payload(raw_pagination) if raw_pagination is not None else None
            if pagination is not None and pagination.seller_id != lease.payload["platform_shop_id"]:
                raise TaskFailure(Failure("invalid_pagination_task", "分页游标的店铺作用域不一致。"))
            snapshot = await TikTokShopAdapter(context.gate, context.settings).discover_products(source, pagination, context.stop)
        except (KeyError, ValueError, TypeError, AttributeError):
            raise TaskFailure(Failure("invalid_task_payload", "商品任务参数校验失败。")) from None
        except PublicPageError as error:
            path = await asyncio.to_thread(save_product_failure, error, context.settings.evidence_directory / lease.task_id)
            diagnostics = getattr(error, "diagnostics", {})
            evidence = {key: diagnostics.get(key) for key in ("request_url", "response_hash", "response_bytes")}
            evidence["evidence_path"] = str(path)
            if error.code == "access_limited":
                await pause_access_scope(context, error.status)
                raise TaskFailure(Failure(error.code, access_message(context, error.status), True,
                                          context.settings.access_pause_seconds, error.status, **evidence)) from None
            raise TaskFailure(Failure(error.code, "公开商品列表或分页校验失败。", error.retryable,
                                      response_status=error.status, **evidence)) from None
        path = await asyncio.to_thread(snapshot.save_evidence, context.settings.evidence_directory / lease.task_id)
        checkpoint = {"root_task_id": root, "page_number": page_number, "has_more": snapshot.feed.has_more,
                      "complete": not snapshot.feed.has_more, "evidence_path": str(path), "response_hash": snapshot.response_hash}

        def apply(session, task):
            summary = persist_product_page(session, task, snapshot, page_number=page_number, root_task_id=root, max_pages=max_pages)
            checkpoint.update(summary)

        return ExecutionResult(apply=apply, checkpoint=checkpoint, request_url=snapshot.final_url,
                               request_method="POST" if pagination is not None else "GET",
                               response_status=snapshot.status, response_bytes=snapshot.response_bytes,
                               response_hash=snapshot.response_hash, latency_ms=snapshot.latency_ms, evidence_path=str(path))


class ProductDetailHandler:
    task_type = TaskType.PRODUCT_DETAIL

    def commit_observation(self, session, task, snapshot, context):
        summary = persist_product_detail(session, task, snapshot)
        if context.settings.baseline_on_detail and not summary.get("stale_observation_skipped"):
            summary.update(persist_snapshot(session, task, snapshot, update_current=False))
        return summary

    async def prepare(self, lease: TaskLease, context: HandlerContext) -> ExecutionResult:
        try:
            payload = lease.payload
            if payload.get("product_id") != lease.resource_id or not lease.resource_id.isdecimal():
                raise TaskFailure(Failure("invalid_detail_task_payload", "详情任务资源标识不一致。"))
            source = public_url(payload["product_url"])
            if (product_id_from_url(source) != identifier(payload["platform_product_id"])
                    or payload.get("pricing_context", "public_listed") != "public_listed"):
                raise TaskFailure(Failure("invalid_detail_task_payload", "详情链接身份或价格上下文不一致。"))
            expected_shop_id = identifier(payload["platform_shop_id"])
            mode = payload.get("capture_mode", "full")
            if mode not in {"full", *BACKFILL_MODES} or mode in BACKFILL_MODES and (
                    self.task_type != TaskType.PRODUCT_DETAIL or payload.get("image_source_version") != IMAGE_SOURCE_VERSION
                    or mode == MEDIA_MODE and payload.get("content_source_version") != CONTENT_SOURCE_VERSION):
                raise TaskFailure(Failure("invalid_capture_mode", "详情采集模式或图片解析版本不正确。"))
            if mode in BACKFILL_MODES:
                if context.session_factory is None:
                    raise TaskFailure(Failure("image_session_unavailable", "图片补采需要显式会话工厂。"))
                available = await context.database.read(image_capture_state, int(lease.resource_id), payload, lease.platform, lease.region)
                if available:
                    def skip(session, task):
                        if not image_capture_state(session, int(task.resource_id), task.payload, task.platform, task.region):
                            raise TaskFailure(Failure("image_skip_state_changed", "图库在跳过确认前变化，等待重试。", True))
                    return ExecutionResult(apply=skip, checkpoint={"capture_mode": mode,
                        "image_source_version": IMAGE_SOURCE_VERSION, "outcome": "skipped_already_available", "images_upserted": 0})
            snapshot = await TikTokShopAdapter(context.gate, context.settings).fetch_product_detail(source, expected_shop_id, context.stop)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise TaskFailure(Failure("invalid_detail_task_payload", "详情任务参数校验失败。")) from None
        except PublicPageError as error:
            path = await asyncio.to_thread(save_product_failure, error, context.settings.evidence_directory / lease.task_id, parser_version=DETAIL_PARSER_VERSION)
            diagnostics = getattr(error, "diagnostics", {})
            evidence = {key: diagnostics.get(key) for key in ("request_url", "response_hash", "response_bytes")}
            evidence["evidence_path"] = str(path)
            if error.code == "access_limited":
                await pause_access_scope(context, error.status)
                raise TaskFailure(Failure(error.code, access_message(context, error.status), True,
                                          context.settings.access_pause_seconds, error.status, **evidence)) from None
            raise TaskFailure(Failure(error.code, "公开详情或 SKU 校验失败。", error.retryable,
                                      response_status=error.status, **evidence)) from None
        path = (await asyncio.to_thread(save_image_evidence, snapshot, context.settings.evidence_directory / lease.task_id, capture_mode=mode) if mode in BACKFILL_MODES
                else await asyncio.to_thread(snapshot.save_evidence, context.settings.evidence_directory / lease.task_id))
        checkpoint = {"evidence_path": str(path), "response_hash": snapshot.response_hash}

        def apply(session, task):
            checkpoint.update(persist_image_observation(session, task, snapshot) if mode in BACKFILL_MODES
                              else self.commit_observation(session, task, snapshot, context))
            ensure_initial_review_task(session, context.settings, int(task.resource_id))

        return ExecutionResult(apply=apply, checkpoint=checkpoint, request_url=snapshot.final_url,
                               response_status=snapshot.status, response_bytes=snapshot.response_bytes,
                               response_hash=snapshot.response_hash, latency_ms=snapshot.latency_ms, evidence_path=str(path))


class ProductSnapshotHandler(ProductDetailHandler):
    task_type = TaskType.PRODUCT_SNAPSHOT

    def commit_observation(self, session, task, snapshot, context):
        return persist_snapshot(session, task, snapshot, interval_seconds=context.settings.snapshot_interval_seconds)


class ChangeDetectionHandler:
    task_type = TaskType.CHANGE_DETECTION

    async def prepare(self, lease: TaskLease, context: HandlerContext) -> ExecutionResult:
        checkpoint = {}
        def apply(session, task):
            checkpoint.update(detect_snapshot_changes(session, task))
        return ExecutionResult(apply=apply, checkpoint=checkpoint)


class ReportGenerationHandler:
    task_type = TaskType.REPORT_GENERATION

    async def prepare(self, lease: TaskLease, context: HandlerContext) -> ExecutionResult:
        if lease.resource_id != "clothing_monitor" or lease.payload.get("report_type") != "clothing_monitor":
            raise TaskFailure(Failure("invalid_report_task", "报告任务类型或资源标识不一致。"))
        hours = lease.payload.get("lookback_hours", 24)
        if type(hours) is not int or not 1 <= hours <= 720:
            raise TaskFailure(Failure("invalid_report_task", "报告观察区间不正确。"))
        if (lease.payload.get("pricing_context") != context.settings.pricing_context
                or lease.payload.get("currency") != context.settings.currency):
            raise TaskFailure(Failure("invalid_report_task", "报告价格上下文或币种与当前配置不一致。"))
        if context.session_factory is None:
            raise TaskFailure(Failure("report_session_unavailable", "报告处理器需要显式配置只读会话工厂。"))
        # SQL and export yield while the independent coroutine renews the lease.
        async with heavy_slot(context.database, context.settings):
            report = await context.database.read(build_monitoring_report, context.settings, lookback_hours=hours)
            artifacts = await asyncio.to_thread(export_report, report, context.settings.report_directory)
        checkpoint = {**artifacts, "report_products": report["summary"]["products"],
                      "report_events": report["summary"]["events_in_window"], "report_as_of": report["as_of"],
                      "report_summary": report["summary"]}
        def apply(session, task):
            if task.resource_type != ResourceType.REPORT:
                raise TaskFailure(Failure("invalid_report_task", "报告任务资源类型不正确。"))
        return ExecutionResult(apply=apply, checkpoint=checkpoint)


def async_handler_registry():
    return {TaskType.PRODUCT_REVIEWS: ProductReviewsHandler(), TaskType.SHOP_DISCOVERY: ShopDiscoveryHandler(), TaskType.PRODUCT_DISCOVERY: ProductDiscoveryHandler(),
            TaskType.SHOP_PROFILE: ShopProfileHandler(),
            TaskType.PRODUCT_DETAIL: ProductDetailHandler(), TaskType.PRODUCT_SNAPSHOT: ProductSnapshotHandler(),
            TaskType.CHANGE_DETECTION: ChangeDetectionHandler(), TaskType.REPORT_GENERATION: ReportGenerationHandler()}



class ProductReviewsHandler:
    async def prepare(self, lease, context):
        from app.workers.reviews import ReviewHandler
        return await ReviewHandler().prepare(lease, context.settings, context.gate)
