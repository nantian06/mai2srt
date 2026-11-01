"""What the GUI task monitor relies on from the job API.

The monitor (and the transcribe page coming back from another page, or a
reloaded webview) rebuilds its view from the server alone: the listing must
say when a job started/ended and what it was started with, a job's event
history must replay in full -- or from an offset, to resume a dropped stream
without duplicates -- and settled jobs can be removed from the history.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from mai2srt.config import Config, set_projects_dir  # noqa: E402
from mai2srt.server.app import create_app  # noqa: E402
from mai2srt.server.jobs import Job, JobManager  # noqa: E402
from test_batch import Fake, touch  # noqa: E402


def test_summary_carries_timings_and_meta():
    async def go() -> tuple[dict, dict]:
        mgr = JobManager()

        async def work(job: Job) -> dict:
            mgr.emit(job, "log", {"line": "hi"})
            return {}

        job = await mgr.start("run", "a.mp3", work,
                              meta={"audio_path": "/x/a.mp3", "use_llm": True})
        before = job.summary()
        await job.task
        return before, job.summary()

    before, after = asyncio.run(go())
    assert before["status"] == "running" and before["finished_at"] is None
    assert before["meta"] == {"audio_path": "/x/a.mp3", "use_llm": True}
    assert after["status"] == "done"
    assert after["finished_at"] >= after["created_at"]
    assert after["events"] == 2                       # log + done


def test_forget_only_drops_settled_jobs():
    async def go() -> tuple[bool, bool, list[str]]:
        mgr = JobManager()
        gate = asyncio.Event()

        async def work(job: Job) -> dict:
            await gate.wait()
            return {}

        job = await mgr.start("run", "t", work)
        while_running = mgr.forget(job.id)
        gate.set()
        await job.task
        after = mgr.forget(job.id)
        return while_running, after, [j.id for j in mgr.list()]

    while_running, after, left = asyncio.run(go())
    assert while_running is False
    assert after is True
    assert left == []


def test_cancel_stamps_the_end_time():
    async def go() -> Job:
        mgr = JobManager()

        async def work(job: Job) -> dict:
            await asyncio.sleep(10)
            return {}

        job = await mgr.start("run", "t", work)
        await asyncio.sleep(0)
        mgr.cancel(job.id)
        await job.task
        return job

    job = asyncio.run(go())
    assert job.status == "cancelled" and job.finished_at is not None


# ------------------------------------------------------------ HTTP surface

@pytest.fixture
def client(tmp_path):
    cfg = Config(data_dir=tmp_path / "data")
    cfg.ensure_dirs()
    set_projects_dir(cfg, tmp_path / "lib")
    with TestClient(create_app(cfg)) as c:
        yield c, tmp_path


def _events(c, job_id, from_idx=0) -> list[str]:
    text = c.get("/api/jobs/%s/events" % job_id, params={"from_idx": from_idx}).text
    return [b.split("\n")[0][len("event: "):] for b in text.strip().split("\n\n") if b]


def _settle(c, job_id, timeout=5.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        j = next(j for j in c.get("/api/jobs").json()["jobs"] if j["id"] == job_id)
        if j["status"] in ("done", "error", "cancelled"):
            return j
        time.sleep(0.02)
    raise AssertionError("job did not settle")


def test_listing_replay_resume_and_forget(client, monkeypatch):
    c, tmp = client
    Fake(monkeypatch)
    a, b = touch(tmp / "a.mp3"), touch(tmp / "b.mp3")
    r = c.post("/api/jobs", json={"kind": "batch", "audio_paths": [str(a), str(b)],
                                  "use_llm": False})
    assert r.status_code == 200, r.text
    created = r.json()["job"]
    assert created["meta"]["audio_paths"] == [str(a), str(b)]
    assert created["meta"]["use_llm"] is False
    assert "created_at" in created

    listed = _settle(c, created["id"])
    assert listed["finished_at"] is not None
    assert listed["meta"] == created["meta"]

    full = _events(c, created["id"])
    assert full[0] == "plan" and full[-1] == "done"
    assert listed["events"] == len(full)
    # a dropped stream resumes where it stopped: no replayed duplicates
    assert _events(c, created["id"], from_idx=len(full) - 1) == ["done"]

    assert c.delete("/api/jobs/%s" % created["id"]).json() == {"ok": True}
    assert all(j["id"] != created["id"] for j in c.get("/api/jobs").json()["jobs"])
    assert c.delete("/api/jobs/%s" % created["id"]).status_code == 404

