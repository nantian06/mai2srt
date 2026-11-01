"""Batch transcription: many media files, one browser session.

Shared by the CLI (``mai2srt batch``) and, later, the UI server. The shape is
deliberately split in two so a ``--dry-run`` is just "plan, print, stop":

    plan_batch()  -> [BatchItem]   pure decisions: what to transcribe, what to
                                   only re-segment, what is already done
    run_batch()   -> BatchReport   executes the plan, one shared browser

Error policy (decided with the user, see the design notes in the PR):

* per-file errors (bad media, one failed upload) are recorded and the batch
  carries on;
* global errors (not signed in / session expired, the 451 biometric gate, no
  browser) abort the whole batch -- every further file would fail the same
  way, and the message says how to fix it;
* a file whose transcript already exists but whose SRT does not is only
  re-segmented: the upload is the slow, quota-consuming step and is never
  repeated needlessly.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import storage
from .config import Config, projects_dir
from .postprocess import PostprocessParams
from .segment import SegmentParams
from .transcribe.browser import NoDisplayError
from .transcribe.client import BiometricConsentRequired

log = logging.getLogger("mai2srt.batch")

#: same curated lists as the desktop app's drop zone (Transcribe.tsx); the
#: pipeline itself is container-agnostic, these only drive directory scans
AUDIO_EXTS = ("mp3", "wav", "m4a", "flac", "ogg", "opus", "aac", "wma")
VIDEO_EXTS = ("mp4", "mkv", "webm", "mov", "m4v", "avi", "wmv", "ts", "flv")
MEDIA_EXTS = frozenset(AUDIO_EXTS + VIDEO_EXTS)

#: parallel uploads share one account and one browser; keep the ceiling low
MAX_JOBS = 3

# action: what the plan intends for a file
ACT_TRANSCRIBE = "transcribe"   # upload + segment + export
ACT_RESEGMENT = "resegment"     # transcript exists; rebuild the SRT only
ACT_SKIP = "skip"               # already complete

# status: what happened
ST_PENDING = "pending"
ST_RUNNING = "running"
ST_DONE = "done"
ST_SKIPPED = "skipped"
ST_FAILED = "failed"
ST_CANCELLED = "cancelled"


class BatchAborted(RuntimeError):
    """A global condition ended the batch; ``str(e)`` says how to fix it."""


@dataclass
class BatchItem:
    audio: Path
    srt: Path
    json_path: Path
    action: str
    reason: str = ""            # why this action (shown by --dry-run)
    status: str = ST_PENDING
    detail: str = ""            # error text / extra info
    llm_note: str | None = None
    words: int = 0
    entries: int = 0
    dialogue: int = 0
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "audio": str(self.audio), "srt": str(self.srt),
            "json": str(self.json_path), "action": self.action,
            "status": self.status, "detail": self.detail,
            "llm_note": self.llm_note, "words": self.words,
            "entries": self.entries, "dialogue": self.dialogue,
            "seconds": round(self.seconds, 1),
        }


@dataclass
class BatchReport:
    items: list[BatchItem]
    aborted: str | None = None      # global-error message, if the batch stopped
    interrupted: bool = False
    #: inputs that could not be collected (missing path, empty folder); the
    #: files that WERE found still run, but the batch is not "ok"
    input_problems: list[str] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for i in self.items if i.status == status)

    @property
    def ok(self) -> bool:
        return (self.aborted is None and not self.interrupted
                and not self.input_problems and self.count(ST_FAILED) == 0)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "aborted": self.aborted,
            "interrupted": self.interrupted,
            "input_problems": list(self.input_problems),
            "done": self.count(ST_DONE), "skipped": self.count(ST_SKIPPED),
            "failed": self.count(ST_FAILED),
            "cancelled": self.count(ST_CANCELLED),
            "pending": self.count(ST_PENDING),
            "llm_fallback": sum(1 for i in self.items if i.llm_note),
            "items": [i.to_dict() for i in self.items],
        }


# ---------------------------------------------------------------------------
# input collection
# ---------------------------------------------------------------------------

def is_media(p: Path) -> bool:
    return p.suffix.lstrip(".").lower() in MEDIA_EXTS


def collect_inputs(paths: list[str | Path], recursive: bool = False
                   ) -> tuple[list[Path], list[str]]:
    """Expand files/directories into an ordered, de-duplicated media list.

    Explicit files are taken as given (ffprobe is the judge of what is media);
    directories contribute the files with a known media extension, sorted by
    name, descending into subfolders only with ``recursive``. Returns
    (files, problems) -- a path that does not exist is a problem, not a crash.
    """
    out: list[Path] = []
    seen: set[str] = set()
    problems: list[str] = []

    def add(p: Path) -> None:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            out.append(Path(os.path.abspath(p)))

    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            walker = p.rglob("*") if recursive else p.glob("*")
            found = sorted(
                (c for c in walker
                 if c.is_file() and not c.name.startswith(".") and is_media(c)),
                key=lambda c: str(c).lower())
            if not found:
                problems.append("no media files in %s%s" % (
                    p, "" if recursive else " (use -r to include subfolders)"))
            for c in found:
                add(c)
        elif p.is_file():
            add(p)
        else:
            problems.append("not found: %s" % p)
    return out, problems


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------

def _short_hash(audio: Path) -> str:
    key = os.path.normcase(os.path.abspath(audio))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:6]


def _srt_targets(audios: list[Path], out_dir: Path | None) -> list[Path]:
    """One SRT path per input; never two inputs onto the same file.

    Default: next to each audio (a collision is impossible there -- two
    different files in one folder cannot share a stem... except ``a.mp3`` and
    ``a.wav``). With ``out_dir`` everything shares one folder, so same-stem
    files from different places collide. EVERY member of a colliding group
    gets a short path hash (``clip-3fa9c1.srt``), not just the later ones, so
    the names do not depend on which file happened to be listed first.
    """
    def raw(a: Path) -> Path:
        return (out_dir if out_dir else a.parent) / (a.stem + ".srt")

    groups: dict[str, list[int]] = {}
    for i, a in enumerate(audios):
        groups.setdefault(os.path.normcase(str(raw(a))), []).append(i)
    targets = [raw(a) for a in audios]
    for idxs in groups.values():
        if len(idxs) > 1:
            for i in idxs:
                a = audios[i]
                targets[i] = raw(a).with_name("%s-%s.srt" % (a.stem, _short_hash(a)))
    return targets


def _srt_is_current(srt: Path, transcript: Path) -> bool:
    """Was this SRT written for (at least) the transcript next to it?

    A run writes the transcript first and the SRT last, so a finished file has
    an SRT that is not older than its transcript. An older SRT is a leftover
    from before a re-transcription that then failed or was cancelled between
    the two writes: counting it as done would skip the file for good.
    """
    try:
        return srt.stat().st_mtime >= transcript.stat().st_mtime
    except OSError:
        return False


def plan_batch(cfg: Config, audios: list[Path], out_dir: Path | None = None,
               overwrite: bool = False) -> list[BatchItem]:
    """Decide, per file, whether to transcribe, re-segment or skip.

    Read-only: nothing is created on disk (the projects folder included), so
    ``--dry-run`` leaves no trace.
    """
    srts = _srt_targets(audios, out_dir)
    lib = projects_dir(cfg)
    reserved: set[str] = set()      # .mai.json paths already promised in this plan

    def key(p: Path) -> str:
        return os.path.normcase(str(p))

    # existing projects first, so a new file's sibling name cannot take one
    existing: list[Path | None] = [storage.find_project(cfg, a) for a in audios]
    for p in existing:
        if p is not None:
            reserved.add(key(p))

    items: list[BatchItem] = []
    for audio, srt, project in zip(audios, srts, existing):
        if project is not None:
            json_path = project
        else:
            json_path = lib / (audio.stem + ".mai.json")
            if key(json_path) in reserved or json_path.exists():
                # someone else's transcript (or an earlier file of this very
                # batch) owns the name: number ours rather than clobber it
                json_path = storage.unique_target(lib, audio.stem + ".mai.json")
                while key(json_path) in reserved:
                    stem = json_path.name[: -len(".mai.json")]
                    json_path = lib / ("%s-%s.mai.json" % (stem, _short_hash(audio)))
            reserved.add(key(json_path))

        if overwrite:
            action, reason = ACT_TRANSCRIBE, "--overwrite"
        elif project is not None and _srt_is_current(srt, project):
            action, reason = ACT_SKIP, "already done (transcript + %s)" % srt.name
        elif project is not None:
            action, reason = ACT_RESEGMENT, (
                "transcript exists, SRT missing" if not srt.exists()
                else "transcript is newer than %s" % srt.name)
        elif srt.exists():
            action, reason = ACT_TRANSCRIBE, "no transcript; will replace existing %s" % srt.name
        else:
            action, reason = ACT_TRANSCRIBE, "new"
        items.append(BatchItem(audio=audio, srt=srt, json_path=json_path,
                               action=action, reason=reason))
    return items


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------

#: (index, total, item) -- a file is about to start / has just settled
ItemCb = Callable[[int, int, BatchItem], None]
#: (item, stage, detail) / (item, line)
ItemStageCb = Callable[[BatchItem, str, str], None]
ItemLogCb = Callable[[BatchItem, str], None]
#: (item, RunResult) -- a file just succeeded; the caller may persist extras
#: (the UI stores the LLM segmentation as a refine record). A failure here is
#: logged, never the file's failure: its subtitles are already written.
ResultCb = Callable[[BatchItem, object], None]


def _noop(*_a, **_k) -> None:
    pass


async def run_batch(
    cfg: Config,
    report: BatchReport,
    *,
    keep: bool = False,
    include_raw: bool = False,
    segment_params: SegmentParams | None = None,
    post_params: PostprocessParams | None = None,
    use_llm: bool = True,
    jobs: int = 1,
    on_item_start: ItemCb = _noop,
    on_item_done: ItemCb = _noop,
    on_stage: ItemStageCb = _noop,
    on_log: ItemLogCb = _noop,
    on_result: ResultCb = _noop,
    on_consent=None,
) -> BatchReport:
    """Execute ``report.items`` in place; returns ``report``.

    The report is created by the caller and mutated as files settle, so a
    Ctrl-C (CancelledError) still leaves an accurate partial summary behind.
    A global error does not raise: it lands in ``report.aborted``.
    """
    from .runner import AuthRequired, browser_session, resegment_existing, run_full
    from .transcribe import browser

    jobs = max(1, min(int(jobs), MAX_JOBS))
    total = len(report.items)
    todo = [(i, it) for i, it in enumerate(report.items)
            if it.action != ACT_SKIP]
    for it in report.items:
        if it.action == ACT_SKIP:
            it.status = ST_SKIPPED
            it.detail = it.reason

    async def one(idx: int, item: BatchItem, page) -> None:
        item.status = ST_RUNNING
        on_item_start(idx, total, item)
        t0 = time.monotonic()
        cb_stage = lambda s, d: on_stage(item, s, d)  # noqa: E731
        cb_log = lambda m: on_log(item, m)            # noqa: E731
        try:
            if item.action == ACT_RESEGMENT:
                res = await resegment_existing(
                    cfg, item.json_path, item.srt,
                    segment_params=segment_params, post_params=post_params,
                    use_llm=use_llm, on_stage=cb_stage, on_log=cb_log)
            else:
                res = await run_full(
                    cfg, item.audio, json_path=item.json_path,
                    keep=keep, include_raw=include_raw,
                    segment_params=segment_params, post_params=post_params,
                    use_llm=use_llm, on_stage=cb_stage, on_log=cb_log,
                    on_consent=on_consent, page=page, srt_path=item.srt)
            item.status = ST_DONE
            item.words, item.entries = res.words, res.entries
            item.dialogue, item.llm_note = res.dialogue, res.llm_note
            try:
                on_result(item, res)
            except Exception as e:  # noqa: BLE001
                log.warning("post-processing %s failed: %s", item.audio.name, e)
        except asyncio.CancelledError:
            item.status = ST_CANCELLED
            raise
        except BiometricConsentRequired as e:
            item.status, item.detail = ST_FAILED, str(e)
            raise BatchAborted(str(e)) from e
        except (AuthRequired, NoDisplayError) as e:
            item.status, item.detail = ST_FAILED, str(e)
            raise BatchAborted(str(e)) from e
        except Exception as e:  # noqa: BLE001 -- one file's failure, whatever its type
            item.status = ST_FAILED
            item.detail = str(e) or type(e).__name__
            # a per-file failure that is really "the session died": probe it
            # (cheap) so the rest of the batch does not fail 40 times over
            if page is not None and item.action == ACT_TRANSCRIBE:
                if await _session_dead(page, cfg, browser):
                    raise BatchAborted(
                        "登录失效，批量已中止：请重新登录后再运行（已完成的文件会被跳过）"
                        " (session expired mid-batch; sign in again and re-run, "
                        "finished files are skipped)") from e
        finally:
            item.seconds = time.monotonic() - t0
            on_item_done(idx, total, item)

    async def drive(page) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        for pair in todo:
            queue.put_nowait(pair)

        async def worker() -> None:
            while True:
                try:
                    idx, item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await one(idx, item, page)

        tasks = [asyncio.ensure_future(worker()) for _ in range(jobs)]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    try:
        if any(it.action == ACT_TRANSCRIBE for _, it in todo):
            # the browser is only started when something actually needs an
            # upload: a resume that merely re-segments has no use for one
            try:
                async with browser_session(cfg, lambda m: on_log(_BATCH_ITEM, m)) as page:
                    await drive(page)
            except BatchAborted:
                raise
            except (AuthRequired, NoDisplayError) as e:
                raise BatchAborted(str(e)) from e
            except RuntimeError as e:       # no usable browser could be launched
                raise BatchAborted(str(e)) from e
        else:
            await drive(None)
    except BatchAborted as e:
        report.aborted = str(e)
    except asyncio.CancelledError:
        report.interrupted = True
        for it in report.items:
            if it.status in (ST_RUNNING, ST_PENDING):
                it.status = ST_CANCELLED
        raise
    if report.aborted:
        for it in report.items:
            if it.status == ST_PENDING:
                it.status = ST_CANCELLED
                it.detail = "batch aborted"
    return report


#: placeholder item for log lines that belong to the batch, not to a file
_BATCH_ITEM = BatchItem(audio=Path("(batch)"), srt=Path(), json_path=Path(),
                        action=ACT_SKIP)


async def _session_dead(page, cfg: Config, browser) -> bool:
    """True when the playground session is gone and cannot be restored."""
    try:
        if await browser.auth_status(page) == 200:
            return False
        return await browser.ensure_auth(page, cfg, interactive=False) != 200
    except Exception:  # noqa: BLE001 -- an unprobeable page is not "dead"
        return False


def format_plan(items: list[BatchItem]) -> str:
    """The ``--dry-run`` listing: one line per file with its intended action."""
    w = max([len(i.action) for i in items] + [6])
    lines = ["%-*s  %s  (%s)" % (w, i.action, i.audio, i.reason) for i in items]
    n = {a: sum(1 for i in items if i.action == a)
         for a in (ACT_TRANSCRIBE, ACT_RESEGMENT, ACT_SKIP)}
    lines.append("plan: %d to transcribe, %d to re-segment, %d to skip" % (
        n[ACT_TRANSCRIBE], n[ACT_RESEGMENT], n[ACT_SKIP]))
    return "\n".join(lines)


def format_summary(report: BatchReport) -> str:
    """End-of-run table: one line per file, then the totals."""
    marks = {ST_DONE: "ok", ST_SKIPPED: "skip", ST_FAILED: "FAIL",
             ST_CANCELLED: "--", ST_PENDING: "--", ST_RUNNING: "..."}
    lines = []
    for i in report.items:
        extra = i.detail if i.status in (ST_FAILED, ST_SKIPPED, ST_CANCELLED) else (
            "%d lines" % i.entries + (", LLM fallback" if i.llm_note else ""))
        lines.append("%-5s %s%s" % (
            marks.get(i.status, i.status), i.audio,
            "  - " + extra.splitlines()[0][:160] if extra else ""))
    lines.append("")
    lines.append("done %d | skipped %d | failed %d | not run %d | LLM fallback %d" % (
        report.count(ST_DONE), report.count(ST_SKIPPED), report.count(ST_FAILED),
        report.count(ST_CANCELLED) + report.count(ST_PENDING),
        sum(1 for i in report.items if i.llm_note)))
    if report.aborted:
        lines.append("ABORTED: " + report.aborted)
    for msg in report.input_problems:
        lines.append("INPUT NOT PROCESSED: " + msg)
    if report.interrupted:
        lines.append("INTERRUPTED: finished files are kept; re-run the same "
                     "command to continue (finished files are skipped)")
    return "\n".join(lines)
