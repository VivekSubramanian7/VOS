"""Job queue tests.

Two properties carry the design: work is serialised (concurrency 1 preserves the
single-writer assumption of ADR-008), and one failing job never takes the worker down —
otherwise a single bad video would silently stop every future one.
"""

from __future__ import annotations

import asyncio

from vos.jobs import JobQueue


async def test_runs_a_submitted_job():
    queue = JobQueue()
    await queue.start()
    done = asyncio.Event()

    await queue.submit("x", lambda: _set(done))
    await queue.drain()
    await queue.stop()

    assert done.is_set()


async def _set(event: asyncio.Event) -> None:
    event.set()


async def test_jobs_run_one_at_a_time():
    """Concurrency 1 is what keeps graph writes single-writer."""
    queue = JobQueue(concurrency=1)
    await queue.start()
    running = 0
    peak = 0

    async def job() -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1

    for i in range(5):
        await queue.submit(f"job-{i}", job)
    await queue.drain()
    await queue.stop()

    assert peak == 1


async def test_order_is_preserved():
    queue = JobQueue()
    await queue.start()
    seen: list[int] = []

    def make(i: int):
        async def job() -> None:
            seen.append(i)

        return job

    for i in range(5):
        await queue.submit(f"job-{i}", make(i))
    await queue.drain()
    await queue.stop()

    assert seen == [0, 1, 2, 3, 4]


async def test_a_failing_job_does_not_kill_the_worker():
    """One bad video must not silently stop every future one."""
    queue = JobQueue()
    await queue.start()
    survived = asyncio.Event()

    async def boom() -> None:
        raise RuntimeError("bad video")

    await queue.submit("boom", boom)
    await queue.submit("after", lambda: _set(survived))
    await queue.drain()
    await queue.stop()

    assert survived.is_set()


async def test_depth_reports_waiting_work():
    queue = JobQueue()
    assert queue.depth == 0
    await queue.submit("queued", lambda: _set(asyncio.Event()))
    assert queue.depth == 1


async def test_stop_is_idempotent():
    queue = JobQueue()
    await queue.start()
    await queue.stop()
    await queue.stop()
