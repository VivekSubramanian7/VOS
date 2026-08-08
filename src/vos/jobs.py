"""A minimal in-process job queue.

Video distillation takes 20-60 seconds. Running it inline would stall the Telegram
polling loop for that long, so it runs on a worker instead.

**Concurrency defaults to 1 on purpose.** ADR-008 makes the system single-writer, which
is what lets every graph write be reasoned about without locks. One worker preserves
that property while still freeing the loop. Raising it is a real architectural change,
not a tuning knob.

The queue is deliberately *not* durable. Making it durable would add a second source of
truth competing with the journal. Instead, outstanding work is **derived**: a
VideoKnowledge thought with no `:Video` attached has not been processed, so a restart
re-enqueues it (see `Neo4jGraph.unprocessed_video_thoughts`). The journal remains the
only thing that must survive a crash.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

Job = Callable[[], Awaitable[None]]


class JobQueue:
    def __init__(self, concurrency: int = 1) -> None:
        self._queue: asyncio.Queue[tuple[str, Job]] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._concurrency = concurrency

    async def start(self) -> None:
        if self._workers:
            return
        self._workers = [
            asyncio.create_task(self._run(i), name=f"vos-worker-{i}")
            for i in range(self._concurrency)
        ]
        log.info("Job queue started with %d worker(s)", self._concurrency)

    async def submit(self, name: str, job: Job) -> None:
        await self._queue.put((name, job))
        log.info("Queued %s (%d waiting)", name, self._queue.qsize())

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    async def _run(self, index: int) -> None:
        while True:
            name, job = await self._queue.get()
            try:
                await job()
            except asyncio.CancelledError:
                self._queue.task_done()
                raise
            except Exception:  # noqa: BLE001
                # A failing job must never take the worker down with it, or one bad
                # video silently stops every future one.
                log.exception("Job %s failed", name)
            else:
                log.info("Job %s done", name)
            finally:
                self._queue.task_done()

    async def drain(self) -> None:
        """Wait for everything queued to finish. Used by tests and shutdown."""
        await self._queue.join()

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
