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

    async def segment_transcribed(cfg, transcribed, srt_path, **kw):
        kw["on_log"]("hello")
        srt_path.write_text("x", encoding="utf-8")
        return type("R", (), dict(words=1, entries=1, dialogue=0, llm_note=None,
                                  json_path=transcribed.json_path,
                                  entry_list=None, word_refs=None))()

    monkeypatch.setattr(runner, "segment_transcribed", segment_transcribed)
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


# ------------------------------------------------- the queue while it runs

def test_files_dropped_on_a_running_batch_join_it(client, tmp_path, monkeypatch):
    c, _ = client
    fake = Fake(monkeypatch)
    fake.delay = 0.5                 # a request round trip fits in easily
    a = touch(tmp_path / "in" / "a.mp3")
    d = touch(tmp_path / "in" / "d.mp3")
    job_id = start(c, [a])
    time.sleep(0.05)
    r = c.post("/api/jobs/%s/files" % job_id, json={"audio_paths": [str(d), str(a)]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["index"] for i in body["items"]] == [1]       # a is already running
    assert body["ignored"] == 1
    listed = next(j for j in c.get("/api/jobs").json()["jobs"] if j["id"] == job_id)
    assert listed["title"] == "2 files"
    assert listed["meta"]["audio_paths"] == [str(a), str(d)]
    assert settle(c, job_id) == "done"
    result = next(dt for t, dt in events(c, job_id) if t == "done")
    assert result["done"] == 2
    assert [p.name for p, _ in fake.full] == ["a.mp3", "d.mp3"]
    # the appended file announced itself with an index the plan did not have
    assert any(t == "file" and dt["index"] == 1 and dt["status"] == "pending"
               for t, dt in events(c, job_id))
    # a finished batch takes no more files: the client starts a new one
    r = c.post("/api/jobs/%s/files" % job_id, json={"audio_paths": [str(d)]})
    assert r.status_code == 409


def test_a_waiting_file_can_be_taken_out(client, tmp_path, monkeypatch):
    c, _ = client
    fake = Fake(monkeypatch)
    fake.delay = 0.5                 # a request round trip fits in easily
    files = [touch(tmp_path / "in" / f"{n}.mp3") for n in "abc"]
    job_id = start(c, files)
    time.sleep(0.05)
    assert c.delete("/api/jobs/%s/files/0" % job_id).status_code == 409   # running
    assert c.delete("/api/jobs/%s/files/2" % job_id).status_code == 200
    assert settle(c, job_id) == "done"
    evs = events(c, job_id)
    assert any(t == "file" and d["index"] == 2 and d["status"] == "removed" for t, d in evs)
    result = next(d for t, d in evs if t == "done")
    assert [i["index"] for i in result["items"]] == [0, 1]
    assert [p.name for p, _ in fake.full] == ["a.mp3", "b.mp3"]


def test_queue_endpoints_refuse_what_is_not_a_running_batch(client, tmp_path, monkeypatch):
    c, _ = client
    Fake(monkeypatch)
    assert c.post("/api/jobs/nope/files", json={"audio_paths": []}).status_code == 404
    job_id = start(c, [touch(tmp_path / "in" / "a.mp3")])
    settle(c, job_id)
    assert c.delete("/api/jobs/%s/files/0" % job_id).status_code == 409


def test_file_events_carry_the_pipeline_phase(client, tmp_path, monkeypatch):
    c, _ = client
    Fake(monkeypatch)
    job_id = start(c, [touch(tmp_path / "in" / "a.mp3")])
    settle(c, job_id)
    phases = [d["phase"] for t, d in events(c, job_id) if t == "file"]
    for p in ("prepare", "wait_upload", "upload", "wait_segment", "segment"):
        assert p in phases
    assert phases[-1] == ""                       # settled


def test_a_single_file_batch_is_titled_by_its_name(client, tmp_path, monkeypatch):
    c, _ = client
    Fake(monkeypatch)
    job_id = start(c, [touch(tmp_path / "in" / "talk.mp3")])
    listed = next(j for j in c.get("/api/jobs").json()["jobs"] if j["id"] == job_id)
    assert listed["title"] == "talk.mp3"
    settle(c, job_id)


def test_retranscribe_redoes_just_the_named_file(client, tmp_path, monkeypatch):
    c, cfg = client
    fake = Fake(monkeypatch)
    one = touch(tmp_path / "in" / "one.mp3")
    two = touch(tmp_path / "in" / "two.mp3")
    for f in (one, two):
        make_project(cfg, f)
        touch(f.with_suffix(".srt"))
    job_id = start(c, [one, two], overwrite_paths=[str(two)])
    assert settle(c, job_id) == "done"
    assert [p.name for p, _ in fake.full] == ["two.mp3"]
    result = next(d for t, d in events(c, job_id) if t == "done")
    assert (result["done"], result["skipped"]) == (1, 1)


def test_llm_concurrency_setting_roundtrip_and_use(client, tmp_path, monkeypatch):
    c, _ = client
    assert c.get("/api/batch/settings").json() == {"llm_jobs": 1, "max_llm_jobs": 4}
    assert c.put("/api/batch/settings", json={"llm_jobs": 9}).status_code == 400
    assert c.put("/api/batch/settings", json={"llm_jobs": 2}).json()["llm_jobs"] == 2
    assert c.get("/api/batch/settings").json()["llm_jobs"] == 2
    fake = Fake(monkeypatch)
    fake.segment_delay = 0.15
    job_id = start(c, [touch(tmp_path / "in" / f"{n}.mp3") for n in "abc"])
    assert settle(c, job_id) == "done"
    assert fake.seg_peak == 2


def test_retry_redoes_a_failed_file_that_looks_done(client, tmp_path, monkeypatch):
    c, cfg = client
    fake = Fake(monkeypatch)
    one = touch(tmp_path / "in" / "one.mp3")
    make_project(cfg, one)
    touch(one.with_suffix(".srt"))
    job_id = start(c, [one], retry_paths=[str(one)])
    assert settle(c, job_id) == "done"
    assert [p.name for p, _ in fake.full] == ["one.mp3"]
