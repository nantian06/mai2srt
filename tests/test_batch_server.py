"""The ``batch`` job kind of the UI server: plan/file events, per-file error
isolation, global abort, refine hand-off. The playground is faked exactly as
in test_batch.py."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

import mai2srt.runner as runner  # noqa: E402
from mai2srt.config import Config, set_projects_dir  # noqa: E402
from mai2srt.media.probe import MediaError  # noqa: E402
from mai2srt.server.app import create_app  # noqa: E402
from test_batch import Fake, make_project, touch  # noqa: E402


@pytest.fixture
def client(tmp_path):
    cfg = Config(data_dir=tmp_path / "data")
    cfg.ensure_dirs()
    set_projects_dir(cfg, tmp_path / "lib")
    # the context manager keeps ONE event loop alive across requests, so a
    # job started by one call is still running when the next one arrives
    with TestClient(create_app(cfg)) as c:
        yield c, cfg


def events(client, job_id) -> list[tuple[str, dict]]:
    """The job's full SSE history (the stream ends when the job settles)."""
    text = client.get("/api/jobs/%s/events" % job_id).text
    out = []
    for block in text.strip().split("\n\n"):
        lines = block.split("\n")
        out.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    return out


def start(client, files, **extra) -> str:
    r = client.post("/api/jobs", json={
        "kind": "batch", "audio_paths": [str(f) for f in files],
        "use_llm": False, **extra})
    assert r.status_code == 200, r.text
    return r.json()["job"]["id"]


def settle(client, job_id, timeout=5.0) -> str:
    end = time.time() + timeout
    while time.time() < end:
        st = next(j["status"] for j in client.get("/api/jobs").json()["jobs"]
                  if j["id"] == job_id)
        if st in ("done", "error", "cancelled"):
            return st
        time.sleep(0.02)
    raise AssertionError("job did not settle")


def test_batch_job_reports_plan_and_every_file(client, tmp_path, monkeypatch):
    c, cfg = client
    fake = Fake(monkeypatch)
    fake.fail["bad.mp3"] = MediaError("corrupt")
    ok = touch(tmp_path / "in" / "ok.mp3")
    bad = touch(tmp_path / "in" / "bad.mp3")
    done = touch(tmp_path / "in" / "done.mp3")
    make_project(cfg, done)
    touch(done.with_suffix(".srt"))

    job_id = start(c, [ok, bad, done])
    assert settle(c, job_id) == "done"          # a failed file is not a failed job
    evs = events(c, job_id)

    plan = next(d for t, d in evs if t == "plan")["items"]
    assert [p["action"] for p in plan] == ["transcribe", "transcribe", "skip"]
    assert plan[2]["status"] == "skipped"       # visible before anything runs

    last = {}
    for t, d in evs:
        if t == "file":
            last[d["index"]] = d["status"]
    assert last == {0: "done", 1: "failed"}
    result = next(d for t, d in evs if t == "done")
    assert (result["done"], result["failed"], result["skipped"]) == (1, 1, 1)
    assert fake.sessions == 1


def test_log_lines_carry_the_file_position(client, tmp_path, monkeypatch):
    c, _ = client
    Fake(monkeypatch)

    async def run_full(cfg, audio, **kw):
        kw["on_log"]("hello")
        kw["srt_path"].write_text("x", encoding="utf-8")
        return type("R", (), dict(words=1, entries=1, dialogue=0, llm_note=None,
                                  json_path=kw["json_path"], entry_list=None,
                                  word_refs=None))()

    monkeypatch.setattr(runner, "run_full", run_full)
    job_id = start(c, [touch(tmp_path / "in" / "a.mp3")])
    settle(c, job_id)
    lines = [d["line"] for t, d in events(c, job_id) if t == "log"]
    assert "[1/1] hello" in lines


def test_global_problem_ends_the_job_in_error_with_the_fix(client, tmp_path, monkeypatch):
    c, _ = client
    Fake(monkeypatch, session_error=runner.AuthRequired("not signed in"))
    job_id = start(c, [touch(tmp_path / "in" / "a.mp3")])
    assert settle(c, job_id) == "error"
    err = next(d for t, d in events(c, job_id) if t == "error")
    assert "not signed in" in err["message"]
    # the file that never ran is reported, not left dangling as pending
    assert any(t == "file" and d["status"] == "cancelled" for t, d in events(c, job_id))


def test_a_batch_that_only_resegments_needs_no_browser(client, tmp_path, monkeypatch):
    c, cfg = client
    fake = Fake(monkeypatch)
    half = touch(tmp_path / "in" / "half.mp3")
    make_project(cfg, half)
    job_id = start(c, [half])
    assert settle(c, job_id) == "done"
    assert fake.sessions == 0 and len(fake.reseg) == 1


def test_overwrite_redoes_finished_files(client, tmp_path, monkeypatch):
    c, cfg = client
    fake = Fake(monkeypatch)
    done = touch(tmp_path / "in" / "done.mp3")
    make_project(cfg, done)
    touch(done.with_suffix(".srt"))
    job_id = start(c, [done], overwrite=True)
    assert settle(c, job_id) == "done"
    assert [p.name for p, _ in fake.full] == ["done.mp3"]


def test_the_edit_record_hand_off_matches_a_single_run(client, tmp_path, monkeypatch):
    """With the LLM on, a finished file's segmentation is stored as an
    origin=llm refine record; with it off a stale record is removed."""
    c, cfg = client
    Fake(monkeypatch)
    a = touch(tmp_path / "in" / "a.mp3")
    job_id = start(c, [a], use_llm=False)
    assert settle(c, job_id) == "done"
    project = next((tmp_path / "lib").glob("*.mai.json"))
    stale = project.with_name(project.name[: -len(".json")] + ".edit.json")
    stale.write_text("{}", encoding="utf-8")
    job_id = start(c, [a], use_llm=False, overwrite=True)
    assert settle(c, job_id) == "done"
    assert not stale.exists()


def test_bad_input_is_refused_up_front(client, tmp_path):
    c, _ = client
    r = c.post("/api/jobs", json={"kind": "batch", "audio_paths": [str(tmp_path / "nope.mp3")]})
    assert r.status_code == 400
    r = c.post("/api/jobs", json={"kind": "batch", "audio_paths": []})
    assert r.status_code == 400


def test_a_second_job_is_refused_while_a_batch_runs(client, tmp_path, monkeypatch):
    c, _ = client
    fake = Fake(monkeypatch)
    fake.delay = 0.3
    job_id = start(c, [touch(tmp_path / "in" / "a.mp3")])
    r = c.post("/api/jobs", json={"kind": "batch", "audio_paths": [str(tmp_path / "in" / "a.mp3")]})
    assert r.status_code == 409
    settle(c, job_id)
