"""Batch transcription: planning, resume, error policy, CLI.

The playground (browser, network) is faked at the two seams run_batch uses --
``runner.browser_session`` and ``runner.run_full`` / ``resegment_existing`` --
so every decision the batch makes runs for real.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mai2srt.batch as b  # noqa: E402
import mai2srt.runner as runner  # noqa: E402
from mai2srt import cli  # noqa: E402
from mai2srt.config import Config, set_projects_dir  # noqa: E402
from mai2srt.media.probe import MediaError  # noqa: E402
from mai2srt.transcribe.client import (  # noqa: E402
    BiometricConsentRequired, TranscribeError,
)


# ----------------------------------------------------------------- fixtures

@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config(data_dir=tmp_path / "data")
    c.ensure_dirs()
    set_projects_dir(c, tmp_path / "lib")
    return c


def touch(p: Path, text: str = "") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def make_project(cfg: Config, audio: Path, name: str | None = None) -> Path:
    from mai2srt.config import projects_dir
    p = projects_dir(cfg) / (name or audio.stem + ".mai.json")
    return touch(p, json.dumps({"source": str(audio), "words": []}))


def res(**kw):
    base = dict(words=10, entries=3, dialogue=0, llm_note=None,
                entry_list=None, word_refs=None, json_path=None)
    base.update(kw)
    return SimpleNamespace(**base)


class Fake:
    """Stand-ins for the playground seams, recording what was asked of them."""

    def __init__(self, monkeypatch, *, session_error=None):
        self.sessions = 0
        self.full: list[tuple[Path, object]] = []
        self.reseg: list[Path] = []
        self.fail: dict[str, Exception] = {}
        self.delay = 0.0
        self.live = 0
        self.peak = 0
        self.dead = False
        fake = self

        @contextlib.asynccontextmanager
        async def session(cfg, on_log=None):
            fake.sessions += 1
            if session_error:
                raise session_error
            yield "PAGE"

        async def run_full(cfg, audio, **kw):
            fake.full.append((audio, kw["page"]))
            fake.live += 1
            fake.peak = max(fake.peak, fake.live)
            try:
                if fake.delay:
                    await asyncio.sleep(fake.delay)
                if audio.name in fake.fail:
                    raise fake.fail[audio.name]
                # what the real run_full leaves behind: transcript + subtitles
                kw["json_path"].parent.mkdir(parents=True, exist_ok=True)
                kw["json_path"].write_text(
                    json.dumps({"source": str(audio), "words": []}),
                    encoding="utf-8")
                kw["srt_path"].write_text("srt", encoding="utf-8")
                return res(json_path=kw["json_path"])
            finally:
                fake.live -= 1

        async def reseg(cfg, json_path, srt_path, **kw):
            fake.reseg.append(json_path)
            srt_path.write_text("srt", encoding="utf-8")
            return res(entries=7, json_path=json_path)

        monkeypatch.setattr(runner, "browser_session", session)
        monkeypatch.setattr(runner, "run_full", run_full)
        monkeypatch.setattr(runner, "resegment_existing", reseg)
        import mai2srt.transcribe.browser as br

        async def auth_status(page):
            return 401 if fake.dead else 200

        async def ensure_auth(page, cfg, interactive=True, **kw):
            return 401 if fake.dead else 200

        monkeypatch.setattr(br, "auth_status", auth_status)
        monkeypatch.setattr(br, "ensure_auth", ensure_auth)


def run(cfg, items, **kw) -> b.BatchReport:
    report = b.BatchReport(items)
    asyncio.run(b.run_batch(cfg, report, **kw))
    return report


# ------------------------------------------------------- collecting inputs

def test_collect_expands_dirs_dedupes_and_ignores_non_media(tmp_path):
    touch(tmp_path / "d" / "b.mp3")
    touch(tmp_path / "d" / "a.MP4")
    touch(tmp_path / "d" / "notes.txt")
    touch(tmp_path / "d" / ".hidden.mp3")
    touch(tmp_path / "d" / "sub" / "c.wav")
    explicit = touch(tmp_path / "d" / "b.mp3")
    files, problems = b.collect_inputs([tmp_path / "d", explicit])
    assert [f.name for f in files] == ["a.MP4", "b.mp3"]      # sorted, no dup
    assert problems == []
    files, _ = b.collect_inputs([tmp_path / "d"], recursive=True)
    assert [f.name for f in files] == ["a.MP4", "b.mp3", "c.wav"]


def test_collect_reports_missing_and_empty_dirs(tmp_path):
    (tmp_path / "empty").mkdir()
    files, problems = b.collect_inputs([tmp_path / "nope", tmp_path / "empty"])
    assert files == []
    assert any("not found" in p for p in problems)
    assert any("-r" in p for p in problems)


def test_explicit_files_are_accepted_whatever_their_extension(tmp_path):
    f = touch(tmp_path / "recording.xyz")
    files, problems = b.collect_inputs([f])
    assert [x.name for x in files] == ["recording.xyz"] and problems == []


# ----------------------------------------------------------------- planning

def test_plan_new_file_is_transcribed_next_to_the_source(cfg, tmp_path):
    a = touch(tmp_path / "in" / "a.mp3")
    [it] = b.plan_batch(cfg, [a])
    assert it.action == b.ACT_TRANSCRIBE
    assert it.srt == a.with_suffix(".srt")


def test_plan_skips_finished_and_resegments_half_finished(cfg, tmp_path):
    done = touch(tmp_path / "in" / "done.mp3")
    half = touch(tmp_path / "in" / "half.mp3")
    make_project(cfg, done)
    touch(done.with_suffix(".srt"))
    make_project(cfg, half)
    items = {i.audio.name: i for i in b.plan_batch(cfg, [done, half])}
    assert items["done.mp3"].action == b.ACT_SKIP
    assert items["half.mp3"].action == b.ACT_RESEGMENT
    assert items["half.mp3"].json_path.name == "half.mai.json"


def test_plan_overwrite_redoes_everything(cfg, tmp_path):
    a = touch(tmp_path / "in" / "a.mp3")
    make_project(cfg, a)
    touch(a.with_suffix(".srt"))
    [it] = b.plan_batch(cfg, [a], overwrite=True)
    assert it.action == b.ACT_TRANSCRIBE
    assert it.json_path.name == "a.mai.json"      # its own project is reused


def test_plan_is_read_only(cfg, tmp_path):
    from mai2srt.config import projects_dir
    a = touch(tmp_path / "in" / "a.mp3")
    b.plan_batch(cfg, [a], out_dir=tmp_path / "out")
    assert not projects_dir(cfg).exists()
    assert not (tmp_path / "out").exists()


def test_out_dir_collisions_get_a_hash_on_every_member(cfg, tmp_path):
    a = touch(tmp_path / "x" / "clip.mp3")
    c = touch(tmp_path / "y" / "clip.mp3")
    d = touch(tmp_path / "x" / "other.mp3")
    out = tmp_path / "out"
    items = b.plan_batch(cfg, [a, c, d], out_dir=out)
    names = [i.srt.name for i in items]
    assert names[2] == "other.srt"
    assert names[0] != names[1] and names[0].startswith("clip-") and names[1].startswith("clip-")
    # stable: the same file gets the same name whatever else is in the batch
    assert b.plan_batch(cfg, [c, a], out_dir=out)[1].srt.name == names[0]
    assert all(i.srt.parent == out for i in items)


def test_same_stem_different_sources_get_distinct_transcripts(cfg, tmp_path):
    a = touch(tmp_path / "x" / "clip.mp3")
    c = touch(tmp_path / "y" / "clip.mp3")
    make_project(cfg, a)                        # a already owns clip.mai.json
    items = b.plan_batch(cfg, [a, c])
    assert items[0].json_path.name == "clip.mai.json"
    assert items[1].json_path.name != "clip.mai.json"
    assert items[1].json_path.name.endswith(".mai.json")


def test_same_stem_new_files_in_one_batch_do_not_share_a_transcript(cfg, tmp_path):
    a = touch(tmp_path / "x" / "clip.mp3")
    c = touch(tmp_path / "y" / "clip.mp3")
    items = b.plan_batch(cfg, [a, c])
    assert items[0].json_path != items[1].json_path


# --------------------------------------------------------------- execution

def test_one_browser_session_serves_the_whole_batch(cfg, tmp_path, monkeypatch):
    fake = Fake(monkeypatch)
    files = [touch(tmp_path / "in" / f"{n}.mp3") for n in "abc"]
    rep = run(cfg, b.plan_batch(cfg, files))
    assert fake.sessions == 1
    assert [p for p, _ in fake.full] == files
    assert {pg for _, pg in fake.full} == {"PAGE"}
    assert rep.ok and rep.count(b.ST_DONE) == 3
    assert all(f.with_suffix(".srt").exists() for f in files)


def test_a_failing_file_does_not_stop_the_rest(cfg, tmp_path, monkeypatch):
    fake = Fake(monkeypatch)
    fake.fail["b.mp3"] = MediaError("no audio stream in b.mp3")
    files = [touch(tmp_path / "in" / f"{n}.mp3") for n in "abc"]
    rep = run(cfg, b.plan_batch(cfg, files))
    st = {i.audio.name: i.status for i in rep.items}
    assert st == {"a.mp3": b.ST_DONE, "b.mp3": b.ST_FAILED, "c.mp3": b.ST_DONE}
    assert "no audio stream" in rep.items[1].detail
    assert rep.aborted is None and not rep.ok


def test_an_unexpected_exception_is_a_file_failure_not_a_crash(cfg, tmp_path, monkeypatch):
    fake = Fake(monkeypatch)
    fake.fail["a.mp3"] = KeyError("format")
    files = [touch(tmp_path / "in" / f"{n}.mp3") for n in "ab"]
    rep = run(cfg, b.plan_batch(cfg, files))
    assert [i.status for i in rep.items] == [b.ST_FAILED, b.ST_DONE]
    assert rep.items[0].detail


def test_unsigned_session_aborts_before_any_upload(cfg, tmp_path, monkeypatch):
    fake = Fake(monkeypatch, session_error=runner.AuthRequired("not signed in"))
    files = [touch(tmp_path / "in" / f"{n}.mp3") for n in "ab"]
    rep = run(cfg, b.plan_batch(cfg, files))
    assert rep.aborted and "not signed in" in rep.aborted
    assert fake.full == []
    assert rep.count(b.ST_CANCELLED) == 2


def test_biometric_gate_aborts_the_batch(cfg, tmp_path, monkeypatch):
    fake = Fake(monkeypatch)
    fake.fail["a.mp3"] = BiometricConsentRequired("451: accept the notice")
    files = [touch(tmp_path / "in" / f"{n}.mp3") for n in "ab"]
    rep = run(cfg, b.plan_batch(cfg, files))
    assert rep.aborted and "451" in rep.aborted
    assert [p.name for p, _ in fake.full] == ["a.mp3"]      # b never started
    assert rep.items[0].status == b.ST_FAILED
    assert rep.items[1].status == b.ST_CANCELLED


def test_a_file_failure_that_is_really_a_dead_session_aborts(cfg, tmp_path, monkeypatch):
    fake = Fake(monkeypatch)
    fake.fail["a.mp3"] = TranscribeError("all 3 attempts failed: HTTP 401")
    fake.dead = True
    files = [touch(tmp_path / "in" / f"{n}.mp3") for n in "ab"]
    rep = run(cfg, b.plan_batch(cfg, files))
    assert rep.aborted and "mid-batch" in rep.aborted
    assert [p.name for p, _ in fake.full] == ["a.mp3"]


def test_a_healthy_session_keeps_going_after_a_file_failure(cfg, tmp_path, monkeypatch):
    fake = Fake(monkeypatch)
    fake.fail["a.mp3"] = TranscribeError("chunk 0: no word timestamps")
    files = [touch(tmp_path / "in" / f"{n}.mp3") for n in "ab"]
    rep = run(cfg, b.plan_batch(cfg, files))
    assert rep.aborted is None
    assert [i.status for i in rep.items] == [b.ST_FAILED, b.ST_DONE]


def test_resume_resegments_without_a_browser(cfg, tmp_path, monkeypatch):
    fake = Fake(monkeypatch)
    half = touch(tmp_path / "in" / "half.mp3")
    done = touch(tmp_path / "in" / "done.mp3")
    make_project(cfg, half)
    make_project(cfg, done)
    touch(done.with_suffix(".srt"))
    rep = run(cfg, b.plan_batch(cfg, [half, done]))
    assert fake.sessions == 0 and fake.full == []
    assert [p.name for p in fake.reseg] == ["half.mai.json"]
    assert [i.status for i in rep.items] == [b.ST_DONE, b.ST_SKIPPED]
    assert rep.items[0].entries == 7


def test_jobs_run_in_parallel_but_are_capped(cfg, tmp_path, monkeypatch):
    fake = Fake(monkeypatch)
    fake.delay = 0.05
    files = [touch(tmp_path / "in" / f"{n}.mp3") for n in "abcdefgh"]
    rep = run(cfg, b.plan_batch(cfg, files), jobs=99)
    assert fake.peak == b.MAX_JOBS
    assert rep.count(b.ST_DONE) == 8
    fake2 = Fake(monkeypatch)
    fake2.delay = 0.01
    run(cfg, b.plan_batch(cfg, files, overwrite=True), jobs=1)
    assert fake2.peak == 1


def test_cancel_leaves_an_accurate_partial_report(cfg, tmp_path, monkeypatch):
    fake = Fake(monkeypatch)
    fake.delay = 0.2
    files = [touch(tmp_path / "in" / f"{n}.mp3") for n in "ab"]
    report = b.BatchReport(b.plan_batch(cfg, files))

    async def go():
        t = asyncio.ensure_future(b.run_batch(cfg, report))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    asyncio.run(go())
    assert report.interrupted
    assert [i.status for i in report.items] == [b.ST_CANCELLED, b.ST_CANCELLED]


def test_llm_fallback_is_counted(cfg, tmp_path, monkeypatch):
    Fake(monkeypatch)

    async def run_full(cfg, audio, **kw):
        kw["srt_path"].write_text("x", encoding="utf-8")
        return res(llm_note="quota exceeded")

    monkeypatch.setattr(runner, "run_full", run_full)
    a = touch(tmp_path / "in" / "a.mp3")
    rep = run(cfg, b.plan_batch(cfg, [a]))
    assert rep.to_dict()["llm_fallback"] == 1
    assert rep.ok                                  # a fallback is not a failure
    assert "LLM fallback 1" in b.format_summary(rep)


# ---------------------------------------------------------------------- CLI

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MAI2SRT_HOME", str(tmp_path / "home"))
    c = Config()
    c.ensure_dirs()
    set_projects_dir(c, tmp_path / "lib")
    return c


def test_cli_dry_run_prints_the_plan_and_touches_nothing(home, tmp_path, capsys):
    a = touch(tmp_path / "in" / "a.mp3")
    done = touch(tmp_path / "in" / "done.mp3")
    make_project(home, done)
    touch(done.with_suffix(".srt"))
    rc = cli.main(["batch", "--dry-run", str(tmp_path / "in")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "transcribe" in out and "skip" in out
    assert "1 to transcribe, 0 to re-segment, 1 to skip" in out
    assert not a.with_suffix(".srt").exists()


def test_cli_without_inputs_found_is_a_usage_error(home, tmp_path):
    assert cli.main(["batch", str(tmp_path / "nothing")]) == 2


def test_cli_refuses_to_start_when_a_prerequisite_is_missing(home, tmp_path, monkeypatch, caplog):
    import mai2srt.preflight as pf
    monkeypatch.setattr(pf, "missing_requirements", lambda cfg, **k: ["ffmpeg", "session"])
    a = touch(tmp_path / "in" / "a.mp3")
    assert cli.main(["batch", str(a)]) == 1


def test_cli_end_to_end_with_report_and_exit_code(home, tmp_path, monkeypatch, capsys):
    import mai2srt.preflight as pf
    monkeypatch.setattr(pf, "missing_requirements", lambda cfg, **k: [])
    fake = Fake(monkeypatch)
    fake.fail["bad.mp3"] = MediaError("corrupt")
    files = [touch(tmp_path / "in" / n) for n in ("ok.mp3", "bad.mp3")]
    report = tmp_path / "r.json"
    rc = cli.main(["batch", "--report", str(report), str(tmp_path / "in")])
    assert rc == 1                                   # a failure -> non-zero
    data = json.loads(report.read_text("utf-8"))
    assert data["done"] == 1 and data["failed"] == 1 and data["ok"] is False
    assert "FAIL" in capsys.readouterr().out
    # second run: the good file is skipped, the bad one retried
    fake.fail.clear()
    assert cli.main(["batch", str(tmp_path / "in")]) == 0
    assert [p.name for p, _ in fake.full] == ["bad.mp3", "ok.mp3", "bad.mp3"]
