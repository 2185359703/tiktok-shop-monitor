import asyncio
import importlib
import inspect
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select, text

from app.main import create_app
from app.models import CrawlAttempt, CrawlTask, ResourceType, Shop, TaskStatus, TaskType
from app.observability.metrics import Metrics
from app.queue.contracts import TaskMessage
from app.queue.retry_policy import Failure, TaskFailure
from app.reports.cache import AsyncReportCache
from app.services.async_scheduler import AsyncScheduler
from app.services.task_runtime import ExecutionResult
from app.settings import Settings
from app.storage.async_database import AsyncDatabase, make_async_engine
from app.workers.async_handlers import async_handler_registry
from app.workers.async_runtime import AsyncWorker


def test_every_runtime_handler_and_api_route_is_a_coroutine():
    registry = async_handler_registry()
    assert set(registry) == set(TaskType)
    assert all(inspect.iscoroutinefunction(handler.prepare) for handler in registry.values())
    names = ('routes', 'routes_analytics', 'routes_collections', 'routes_content', 'routes_images',
             'routes_matching', 'routes_reports', 'routes_reviews', 'routes_ui_v2')
    for name in names:
        router = importlib.import_module('app.api.' + name).router
        assert all(inspect.iscoroutinefunction(route.endpoint) for route in router.routes)


def test_cancelled_cache_waiter_does_not_cancel_shared_build():
    async def run():
        cache, start, release = AsyncReportCache(), asyncio.Event(), asyncio.Event()
        calls = []
        async def build():
            calls.append(1)
            start.set()
            await release.wait()
            return {'ok': True}
        first = asyncio.create_task(cache.get_or_build('key', build))
        await start.wait()
        second = asyncio.create_task(cache.get_or_build('key', build))
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        release.set()
        assert await second == {'ok': True} and calls == [1]
        await cache.close()
    asyncio.run(run())


def test_native_sql_overlaps_without_a_thread_executor(mysql_engine, monkeypatch):
    async def run():
        database = AsyncDatabase(make_async_engine(mysql_engine.url))
        ticks, owner = [], threading.get_ident()
        async def tick():
            for _ in range(35):
                ticks.append(time.perf_counter())
                await asyncio.sleep(.005)
        def probe(db):
            assert threading.get_ident() == owner
            return db.scalar(text('SELECT SLEEP(0.15)'))
        async def forbidden(*args, **kwargs):
            raise AssertionError('Native SQL must not use to_thread')
        monkeypatch.setattr(asyncio, 'to_thread', forbidden)
        try:
            await database.read(lambda db: db.scalar(text('SELECT 1')))
            start = time.perf_counter()
            result, _ = await asyncio.gather(asyncio.gather(*(database.read(probe) for _ in range(4))), tick())
            assert result == [0] * 4 and time.perf_counter() - start < .6
            assert len(ticks) == 35
        finally:
            await database.close()
    asyncio.run(run())


async def create_tasks(database, count=1):
    def insert(db):
        result = []
        for _ in range(count):
            row = CrawlTask(task_type=TaskType.CHANGE_DETECTION, resource_type=ResourceType.PRODUCT,
                resource_id='1', region='US', payload={}, available_at=datetime.now(timezone.utc))
            db.add(row)
            db.flush()
            result.append(row.task_id)
        return result
    return await database.transaction(insert)


async def cleanup(database, ids):
    def remove(db):
        keys = select(CrawlTask.id).where(CrawlTask.task_id.in_(ids))
        db.execute(delete(CrawlAttempt).where(CrawlAttempt.task_id.in_(keys)))
        db.execute(delete(CrawlTask).where(CrawlTask.task_id.in_(ids)))
    await database.transaction(remove)


def test_concurrent_native_claims_commit_once_and_domain_failure_rolls_back(mysql_engine):
    async def run():
        database = AsyncDatabase(make_async_engine(mysql_engine.url))
        ids = await create_tasks(database, 2)
        inserted_shop = 'async-' + uuid4().hex
        calls = []
        class Handler:
            async def prepare(self, lease, context):
                calls.append(lease.task_id)
                await asyncio.sleep(.04)
                def apply(db, task):
                    if lease.task_id == ids[1]:
                        db.add(Shop(region='US', platform_shop_id=inserted_shop, canonical_name='must roll back'))
                        db.flush()
                        raise TaskFailure(Failure('invalid_fixture', 'fixture validation failure'))
                return ExecutionResult(apply=apply, checkpoint={'applied': True})
        config = Settings(enabled_task_types=(TaskType.CHANGE_DETECTION,), worker_concurrency=4)
        worker = AsyncWorker(config, database, {TaskType.CHANGE_DETECTION: Handler()}, None, 'async-test')
        try:
            assert await asyncio.gather(*(worker.process(TaskMessage(ids[0], TaskType.CHANGE_DETECTION)) for _ in range(8))) == ['ack'] * 8
            assert calls == [ids[0]]
            assert await worker.process(TaskMessage(ids[1], TaskType.CHANGE_DETECTION)) == 'reject'
            def verify(db):
                rows = db.scalars(select(CrawlTask).where(CrawlTask.task_id.in_(ids)).order_by(CrawlTask.id)).all()
                assert [row.status for row in rows] == [TaskStatus.SUCCEEDED, TaskStatus.DEAD]
                assert [row.attempt_count for row in rows] == [1, 1]
                assert db.scalar(select(Shop.id).where(Shop.platform_shop_id == inserted_shop)) is None
            await database.read(verify)
        finally:
            await cleanup(database, ids)
            await database.close()
    asyncio.run(run())


def test_async_scheduler_keeps_backpressure_and_dispatch_dedupe(mysql_engine):
    async def run():
        database = AsyncDatabase(make_async_engine(mysql_engine.url))
        ids = await create_tasks(database, 2)
        class Broker:
            high = True
            sent = []
            async def ready_count(self, kind):
                return 100 if self.high else 0
            async def publish(self, message):
                self.sent.append(message.task_id)
        class Gate:
            keys = set()
            async def pause_remaining(self): return 0
            async def reserve_publish(self, key):
                if key in self.keys: return None
                self.keys.add(key)
                return 'token'
        config = Settings(enabled_task_types=(TaskType.CHANGE_DETECTION,))
        broker = Broker()
        scheduler = AsyncScheduler(config, database, broker, Gate(), Metrics())
        try:
            assert (await scheduler.dispatch_once())['published'] == 0
            broker.high = False
            assert (await scheduler.dispatch_once())['published'] == 2
            assert set(broker.sent) == set(ids)
            assert (await scheduler.dispatch_once())['published'] == 0
        finally:
            await cleanup(database, ids)
            await database.close()
    asyncio.run(run())


def test_real_async_api_reads_auth_and_rollback_boundaries(mysql_engine):
    async def run():
        engine = make_async_engine(mysql_engine.url)
        app = create_app(settings=Settings(), engine=engine)
        try:
            async with app.router.lifespan_context(app):
                assert app.state.database.engine.dialect.driver == 'aiomysql'
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                    replies = await asyncio.gather(*(client.get(path) for path in ('/health/ready','/api/v1/overview','/api/v1/shops','/metrics')))
                    assert all(reply.status_code == 200 for reply in replies)
                    assert (await client.get('/api/v1/products/999999/reviews')).status_code == 404
                    assert (await client.post('/api/v1/products/999999/reviews/collect', json={'max_pages': 3})).status_code == 503
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_cancelled_database_wait_releases_the_connection(mysql_engine):
    async def run():
        database = AsyncDatabase(make_async_engine(mysql_engine.url))
        try:
            pending = asyncio.create_task(database.read(lambda db: db.scalar(text('SELECT SLEEP(2)'))))
            await asyncio.sleep(.05)
            pending.cancel()
            result = await asyncio.gather(pending, return_exceptions=True)
            assert isinstance(result[0], asyncio.CancelledError)
            assert await database.read(lambda db: db.scalar(text('SELECT 1'))) == 1
        finally:
            await database.close()
    asyncio.run(run())


def test_native_api_commits_authenticated_job_and_deduplicates_replay(mysql_engine):
    async def run():
        engine = make_async_engine(mysql_engine.url)
        config = Settings(api_token='test-only-async-token-00000000', enabled_task_types=(TaskType.REPORT_GENERATION,))
        app = create_app(settings=config, engine=engine)
        task_id = None
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                    headers = {'Authorization': 'Bearer ' + config.api_token}
                    body = {'lookback_hours': 2}
                    first = await client.post('/api/v1/reports/jobs', headers=headers, json=body)
                    assert first.status_code == 200, first.text
                    task_id = first.json()['task_id']
                    again = await client.post('/api/v1/reports/jobs', headers=headers, json=body)
                    assert again.json()['task_id'] == task_id and not again.json()['created']
                    row = await app.state.database.read(lambda db: db.scalar(select(CrawlTask).where(CrawlTask.task_id == task_id)))
                    assert row.status == TaskStatus.PENDING
        finally:
            if task_id:
                await cleanup(AsyncDatabase(engine), [task_id])
            await engine.dispose()
    asyncio.run(run())


def test_native_analysis_slots_allow_independent_jobs_and_keep_heavy_slot_exclusive(mysql_engine, monkeypatch):
    from app.models.analytics_read import AnalyticsRefreshRequest as Job
    from app.services.analytics_refresh import claim, enqueue, failed
    from app.services.async_heavy_slot import heavy_slot
    async def run():
        database = AsyncDatabase(make_async_engine(mysql_engine.url))
        config = Settings(analytics_concurrency=2)
        keys = []
        try:
            def seed(db):
                return [enqueue(db, 'product_projection', {'product_id': str(900000 + i)}).key_hash for i in range(2)]
            keys.extend(await database.transaction(seed))
            first, second = await asyncio.gather(
                database.transaction(claim, config, slot_name='analysis_slot:0'),
                database.transaction(claim, config, slot_name='analysis_slot:1'))
            # SKIP LOCKED may defer a competitor while a bounded candidate page
            # is locked; a subsequent claim must still admit the independent job.
            first = first or await database.transaction(claim, config, slot_name='analysis_slot:0')
            second = second or await database.transaction(claim, config, slot_name='analysis_slot:1')
            assert first and second and first.key != second.key and first.slot_key != second.slot_key
            await database.transaction(failed, first, ValueError())
            await database.transaction(failed, second, ValueError())
            monkeypatch.setenv('ANALYTICS_ENABLED', '1')
            async with heavy_slot(database, config):
                with pytest.raises(TaskFailure):
                    async with heavy_slot(database, config):
                        pytest.fail('Only one heavy report/aggregate may own the lease')
            async with heavy_slot(database, config):
                pass
        finally:
            await database.transaction(lambda db: db.execute(delete(Job).where(Job.key_hash.in_(keys))))
            await database.close()
    asyncio.run(run())


def test_async_detail_handler_commits_real_product_projection_and_analysis_job(mysql_engine, monkeypatch, tmp_path):
    from app.models import Product, ProductVariant, ProductImage, ProductContent
    from app.models.analytics_read import AnalyticsRefreshRequest as Job, ProductReadModel
    from app.services.analytics_refresh import enqueue
    from app.services.async_analytics import AsyncAnalyticsRunner
    from test_product_detail_storage import initial
    from test_product_detail import snapshot
    async def fetch(*args, **kwargs):
        await asyncio.sleep(.01)
        return snapshot()
    monkeypatch.setattr('app.workers.async_handlers.TikTokShopAdapter.fetch_product_detail', fetch)
    async def run():
        database = AsyncDatabase(make_async_engine(mysql_engine.url))
        product, task = await database.transaction(initial)
        config = Settings(enabled_task_types=(TaskType.PRODUCT_DETAIL,), evidence_directory=tmp_path)
        class Gate:
            async def pause_remaining(self): return 0
        worker = AsyncWorker(config, database, async_handler_registry(), Gate(), 'native-detail')
        projection_key = None
        try:
            assert await worker.process(TaskMessage(task.task_id, task.task_type)) == 'ack'
            def verify(db):
                current = db.get(Product, product.id)
                assert current.title == snapshot().detail.title and current.last_success_at is not None
                assert len(db.scalars(select(ProductVariant).where(ProductVariant.product_id == product.id)).all()) == 2
                assert db.get(CrawlTask, task.id).checkpoint['detail_updated'] is True
            await database.read(verify)
            row = await database.transaction(enqueue, 'product_projection', {'product_id': str(product.id)})
            projection_key = row.key_hash
            assert await AsyncAnalyticsRunner(database, config).run_once()
            assert await database.read(lambda db: db.scalar(select(ProductReadModel.product_id).where(ProductReadModel.product_id == product.id).limit(1))) == product.id
        finally:
            def remove(db):
                if projection_key:
                    db.execute(delete(Job).where(Job.kind == 'product_projection', Job.key_hash == projection_key))
                db.execute(delete(ProductReadModel).where(ProductReadModel.product_id == product.id))
                for model in (ProductVariant, ProductImage, ProductContent):
                    db.execute(delete(model).where(model.product_id == product.id))
                db.execute(delete(Product).where(Product.id == product.id))
                db.execute(delete(Shop).where(Shop.id == product.shop_id))
            await database.transaction(remove)
            await cleanup(database, [task.task_id])
            await database.close()
    asyncio.run(run())
