"""Shared lifecycle for coroutine workers and scheduler entrypoints."""

import asyncio
import logging
import socket
from uuid import uuid4

from app.queue.async_broker import AsyncRabbitMQ
from app.queue.async_redis_gate import AsyncRedisGate
from app.queue.contracts import TaskMessage
from app.workers.async_runtime import AsyncWorker


async def pause_until_stop(stop, seconds):
    end = asyncio.get_running_loop().time() + seconds
    while not stop.is_set():
        remaining = end - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(.2, remaining))


async def consume(settings, worker, broker, stop, *, once=False):
    """ACK only the delivery's own live channel, after fenced SQL commit."""
    active, consumers = set(), []
    failed = asyncio.Event()

    async def process(delivery, kind):
        try:
            try:
                message = TaskMessage.from_bytes(delivery.body)
                if message.task_type != kind:
                    raise ValueError("route mismatch")
            except ValueError:
                await delivery.reject(requeue=False)
                return
            action = await worker.process(message)
            if action == "ack":
                await delivery.ack()
            else:
                await delivery.reject(requeue=False)
        except Exception as error:
            logging.getLogger(__name__).error("worker_delivery_error", extra={"error_type": type(error).__name__})
            failed.set()

    try:
        if once:
            for kind in settings.enabled_task_types:
                delivery = await broker.get(kind)
                if delivery is not None:
                    await process(delivery, kind)
                    break
            return not failed.is_set()
        for kind in settings.enabled_task_types:
            async def dispatch(delivery, expected=kind):
                task = asyncio.create_task(process(delivery, expected))
                active.add(task)
                task.add_done_callback(active.discard)
            tag = await broker.queues[kind].consume(dispatch)
            consumers.append((broker.queues[kind], tag))
        while not stop.is_set() and not failed.is_set() and broker.connected:
            await asyncio.sleep(.1)
        for queue, tag in consumers:
            if broker.connected:
                await queue.cancel(tag)
        if active:
            _, pending = await asyncio.wait(active, timeout=settings.lease_seconds)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        return not failed.is_set()
    finally:
        for task in tuple(active):
            task.cancel()
        await asyncio.gather(*tuple(active), return_exceptions=True)


async def worker_loop(settings, database, registry, metrics, stop, *, once=False, worker_id=None, runtime_status=None):
    runtime_status = runtime_status if runtime_status is not None else {}
    if not settings.enabled_task_types:
        runtime_status["state"] = "disabled"
        while not stop.is_set() and not once:
            await asyncio.sleep(.2)
        return 0
    settings.require_infrastructure()
    gate = AsyncRedisGate(settings)
    worker = AsyncWorker(settings, database, registry, gate,
                         worker_id or f"{socket.gethostname()}:{uuid4().hex[:12]}"[:128], metrics)
    try:
        while not stop.is_set():
            broker = AsyncRabbitMQ(settings)
            try:
                runtime_status["state"] = "connecting"
                await broker.connect()
                runtime_status["state"] = "running"
                ok = await consume(settings, worker, broker, stop, once=once)
                runtime_status["state"] = "draining" if stop.is_set() else "reconnecting"
                if once:
                    return 0 if ok else 1
            except Exception as error:
                runtime_status["state"] = "reconnecting"
                logging.getLogger(__name__).error("worker_retry_connection", extra={"error_type": type(error).__name__})
                if once:
                    return 1
            finally:
                await broker.close()
            await pause_until_stop(stop, settings.poll_seconds)
        return 0
    finally:
        runtime_status["state"] = "stopped"
        await gate.close()
