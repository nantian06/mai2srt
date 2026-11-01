"""JobManager.stream must not end before the job's terminal event is in.

cancel() flips the status immediately and appends the ``cancelled`` event a
moment later (marshalled onto the loop). A stream that stopped on status alone
could close in that gap; the client then never saw ``cancelled`` and sat on a
stale "running" screen until the browser's EventSource reconnected.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.server.jobs import Job, JobManager  # noqa: E402


async def _collect(mgr: JobManager, job: Job) -> list[str]:
    return [ev["type"] async for ev in mgr.stream(job)]


def test_stream_waits_for_the_terminal_event_after_a_cancel():
    async def go() -> list[str]:
        mgr = JobManager()
        job = Job(id="batch-1", kind="batch", title="t", status="running")
        mgr._emit_now(job, {"type": "file", "data": {}, "ts": 0})
        reader = asyncio.ensure_future(_collect(mgr, job))
        await asyncio.sleep(0.05)
        job.status = "cancelled"            # what cancel() does first...
        mgr._wake(job)
        await asyncio.sleep(0.05)
        assert not reader.done(), "stream ended before the cancelled event"
        mgr._emit_now(job, {"type": "cancelled", "data": {}, "ts": 0})  # ...then this
        return await asyncio.wait_for(reader, 1)

    assert asyncio.run(go()) == ["file", "cancelled"]


def test_stream_still_ends_for_a_job_that_finished_without_a_terminal_event():
    """The worker having wound down is also a valid end (a task cancelled
    before it ever ran emits nothing of its own)."""
    async def go() -> list[str]:
        mgr = JobManager()
        job = Job(id="x-1", kind="run", title="t", status="running")
        mgr._emit_now(job, {"type": "log", "data": {}, "ts": 0})
        reader = asyncio.ensure_future(_collect(mgr, job))
        await asyncio.sleep(0.05)
        job.status, job.finished = "cancelled", True
        mgr._wake(job)
        return await asyncio.wait_for(reader, 1)

    assert asyncio.run(go()) == ["log"]


def test_a_normal_job_still_streams_to_done_and_ends():
    async def go() -> list[str]:
        mgr = JobManager()

        async def work(job: Job) -> dict:
            mgr.emit(job, "log", {"line": "hi"})
            return {"ok": True}

        job = await mgr.start("run", "t", work)
        return await asyncio.wait_for(_collect(mgr, job), 2)

    assert asyncio.run(go()) == ["log", "done"]
