"""Batch transcription: many media files, one browser session.

Shared by the CLI (``mai2srt batch``) and the UI server. The shape is
deliberately split in two so a ``--dry-run`` is just "plan, print, stop":

    plan_batch()  -> [BatchItem]   pure decisions: what to transcribe, what to
                                   only re-segment, what is already done
    BatchQueue    -> BatchReport   executes the plan, one shared browser
    run_batch()                    (the same, for a fixed list)

Execution is a three-stage pipeline, because each stage spends a different
resource and a file used to hold all three while the others sat idle:

    prepare (local ffmpeg) -> upload (playground) -> segment (LLM API)

Each stage has its own workers: one preparer that runs at most PREFETCH file
ahead of the uploads (prepared pieces sit on disk), ``jobs`` uploaders sharing
the browser page, and ``llm_jobs`` segmenters. While file 1 is being
segmented, file 2 uploads and file 3 is prepared. Files that only need
re-segmenting skip straight to the last stage.

The queue stays open while it has work: the UI appends files to a running
batch (``BatchQueue.add``), removes ones that have not started yet, or
re-queues a finished one. It closes once every file has settled.

Error policy (decided with the user, see the design notes in the PR):

* per-file errors (bad media, one failed upload) are recorded and the batch
  carries on;
* global errors (not signed in / session expired, the 451 biometric gate, no
  browser) abort the whole batch -- every further file would fail the same
  way, and the message says how to fix it;
* a file whose transcript already exists but whose SRT does not is only
  re-segmented: the upload is the slow, quota-consuming step and is never
  repeated needlessly;
* after a global error, files already uploaded still get their subtitles:
  only the files that would need the playground are cancelled.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import storage
from .config import MAX_LLM_JOBS, Config, projects_dir
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
#: MAX_LLM_JOBS (from config): files segmented at the same time, each its
#: own run of LLM calls
#: how many files may be prepared ahead of the uploads (their encoded pieces
#: wait on disk, so this also bounds the temporary space a batch uses)
PREFETCH = 1

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
#: taken out of a running queue before it started (UI only)
ST_REMOVED = "removed"

# phase: where a running file is in the pipeline ("" when not running)
PH_PREPARE = "prepare"
PH_WAIT_UPLOAD = "wait_upload"
PH_UPLOAD = "upload"
PH_WAIT_SEGMENT = "wait_segment"
PH_SEGMENT = "segment"


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
    #: stable position in the queue: survives removals and appends
    index: int = -1
    #: where a running file is in the pipeline (PH_*); "" otherwise
    phase: str = ""
    started_at: float = field(default=0.0, repr=False)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "audio": str(self.audio), "srt": str(self.srt),
            "json": str(self.json_path), "action": self.action,
            "status": self.status, "phase": self.phase, "detail": self.detail,
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


def _srt_targets(audios: list[Path], out_dir: Path | None,
                 taken: set[str] | None = None) -> list[Path]:
    """One SRT path per input; never two inputs onto the same file.

    Default: next to each audio (a collision is impossible there -- two
    different files in one folder cannot share a stem... except ``a.mp3`` and
    ``a.wav``). With ``out_dir`` everything shares one folder, so same-stem
    files from different places collide. EVERY member of a colliding group
    gets a short path hash (``clip-3fa9c1.srt``), not just the later ones, so
    the names do not depend on which file happened to be listed first.
    ``taken``: SRT paths a running queue already promised to other files; a
    file appended later that would land on one is hashed the same way.
    """
    def raw(a: Path) -> Path:
        return (out_dir if out_dir else a.parent) / (a.stem + ".srt")

    groups: dict[str, list[int]] = {}
    for i, a in enumerate(audios):
        groups.setdefault(os.path.normcase(str(raw(a))), []).append(i)
    targets = [raw(a) for a in audios]
    for k, idxs in groups.items():
        if len(idxs) > 1 or (taken and k in taken):
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


def _action_for(project: Path | None, srt: Path, overwrite: bool
                ) -> tuple[str, str]:
    """(action, reason) for one file, given its transcript (if any)."""
    if overwrite:
        return ACT_TRANSCRIBE, "--overwrite"
    if project is not None and _srt_is_current(srt, project):
        return ACT_SKIP, "already done (transcript + %s)" % srt.name
    if project is not None:
        return ACT_RESEGMENT, (
            "transcript exists, SRT missing" if not srt.exists()
            else "transcript is newer than %s" % srt.name)
    if srt.exists():
        return ACT_TRANSCRIBE, "no transcript; will replace existing %s" % srt.name
    return ACT_TRANSCRIBE, "new"


def plan_batch(cfg: Config, audios: list[Path], out_dir: Path | None = None,
               overwrite: bool = False, *, reserved: set[str] | None = None,
               taken_srts: set[str] | None = None,
               first_index: int = 0) -> list[BatchItem]:
    """Decide, per file, whether to transcribe, re-segment or skip.

    Read-only: nothing is created on disk (the projects folder included), so
    ``--dry-run`` leaves no trace. ``reserved`` / ``taken_srts``: transcript
    and SRT paths (normcased) a running queue already promised to its files,
    so files appended to it never share one. Items are numbered from
    ``first_index``.
    """
    srts = _srt_targets(audios, out_dir, taken_srts)
    lib = projects_dir(cfg)

    def key(p: Path) -> str:
        return os.path.normcase(str(p))

    # .mai.json paths already promised in this plan (or by the running queue)
    promised: set[str] = set(reserved or ())

    # existing projects first, so a new file's sibling name cannot take one
    existing: list[Path | None] = [storage.find_project(cfg, a) for a in audios]
    for p in existing:
        if p is not None:
            promised.add(key(p))

    items: list[BatchItem] = []
    for n, (audio, srt, project) in enumerate(zip(audios, srts, existing)):
        if project is not None:
            json_path = project
        else:
            json_path = lib / (audio.stem + ".mai.json")
            if key(json_path) in promised or json_path.exists():
                # someone else's transcript (or an earlier file of this very
                # batch) owns the name: number ours rather than clobber it
                json_path = storage.unique_target(lib, audio.stem + ".mai.json")
                while key(json_path) in promised:
                    stem = json_path.name[: -len(".mai.json")]
                    json_path = lib / ("%s-%s.mai.json" % (stem, _short_hash(audio)))
            promised.add(key(json_path))

        action, reason = _action_for(project, srt, overwrite)
        items.append(BatchItem(audio=audio, srt=srt, json_path=json_path,
                               action=action, reason=reason,
                               index=first_index + n))
    return items


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------

#: (index, total, item) -- a file is about to start / has just settled
ItemCb = Callable[[int, int, BatchItem], None]
#: (item,) -- a running file moved to another pipeline phase
PhaseCb = Callable[[BatchItem], None]
#: (item, stage, detail) / (item, line)
ItemStageCb = Callable[[BatchItem, str, str], None]
ItemLogCb = Callable[[BatchItem, str], None]
#: (item, RunResult) -- a file just succeeded; the caller may persist extras
#: (the UI stores the LLM segmentation as a refine record). A failure here is
#: logged, never the file's failure: its subtitles are already written.
ResultCb = Callable[[BatchItem, object], None]

SESSION_DEAD_MSG = (
    "登录失效，批量已中止：请重新登录后再运行（已完成的文件会被跳过）"
    " (session expired mid-batch; sign in again and re-run, "
    "finished files are skipped)")


def _noop(*_a, **_k) -> None:
    pass


def _audio_key(p: Path) -> str:
    return os.path.normcase(os.path.abspath(p))


class BatchQueue:
    """Runs a batch as a pipeline; files can be added while it runs.

    The report is created by the caller and mutated as files settle, so a
    Ctrl-C (CancelledError) still leaves an accurate partial summary behind.
    A global error does not raise: it lands in ``report.aborted``.

    Everything runs on one event loop: ``add`` / ``remove`` are plain calls
    made from that loop, and a queue that has closed (all files settled)
    refuses them, so a file is never added to a batch that already ended.
    """

    def __init__(
        self,
        cfg: Config,
        report: BatchReport,
        *,
        keep: bool = False,
        include_raw: bool = False,
        segment_params: SegmentParams | None = None,
        post_params: PostprocessParams | None = None,
        use_llm: bool = True,
        jobs: int = 1,
        llm_jobs: int = 1,
        out_dir: Path | None = None,
        on_item_start: ItemCb = _noop,
        on_item_done: ItemCb = _noop,
        on_item_phase: PhaseCb = _noop,
        on_stage: ItemStageCb = _noop,
        on_log: ItemLogCb = _noop,
        on_result: ResultCb = _noop,
        on_consent=None,
    ) -> None:
        self.cfg = cfg
        self.report = report
        self.keep = keep
        self.include_raw = include_raw
        self.segment_params = segment_params
        self.post_params = post_params
        self.use_llm = use_llm
        self.jobs = max(1, min(int(jobs), MAX_JOBS))
        self.llm_jobs = max(1, min(int(llm_jobs), MAX_LLM_JOBS))
        self.out_dir = out_dir
        self.on_item_start = on_item_start
        self.on_item_done = on_item_done
        self.on_item_phase = on_item_phase
        self.on_stage = on_stage
        self.on_log = on_log
        self.on_result = on_result
        self.on_consent = on_consent

        self._prep_q: asyncio.Queue = asyncio.Queue()
        self._upload_q: asyncio.Queue = asyncio.Queue()
        self._seg_q: asyncio.Queue = asyncio.Queue()
        # a slot is held from the start of a file's preparation until its
        # upload ends: at most `jobs` uploading + PREFETCH prepared ahead
        self._slots = asyncio.Semaphore(self.jobs + PREFETCH)
        self._outstanding = 0
        self._done = asyncio.Event()
        self._closed = False
        self._stop_uploads = False
        self._page = None
        self._page_lock = asyncio.Lock()
        self._stack = contextlib.AsyncExitStack()
        self._next_index = 0
        for it in report.items:
            if it.index < 0:
                it.index = self._next_index
            self._next_index = max(self._next_index, it.index + 1)
            self._admit(it)

    # -------------------------------------------------------------- public

    @property
    def closed(self) -> bool:
        """No more files are accepted (all settled, aborted or stopped)."""
        return self._closed or self.report.aborted is not None

    def add(self, audios: list[Path], overwrite: bool = False
            ) -> list[BatchItem] | None:
        """Append files to the running queue; None when it has closed.

        Returns the items that will run (or were settled at once as skipped):
        new files, plus files already in the queue that are re-queued -- a
        failed or cancelled one always, a finished or skipped one only with
        ``overwrite``. A file still waiting or running is left alone.
        """
        if self.closed:
            return None
        by_key = {_audio_key(it.audio): it for it in self.report.items}
        touched: list[BatchItem] = []
        fresh: list[Path] = []
        seen: set[str] = set()
        for a in audios:
            k = _audio_key(a)
            if k in seen:
                continue
            seen.add(k)
            it = by_key.get(k)
            if it is None:
                fresh.append(Path(os.path.abspath(a)))
            elif it.status in (ST_PENDING, ST_RUNNING):
                continue
            elif overwrite or it.status in (ST_FAILED, ST_CANCELLED):
                self._requeue(it, overwrite)
                touched.append(it)
        if fresh:
            items = plan_batch(
                self.cfg, fresh, out_dir=self.out_dir, overwrite=overwrite,
                reserved={os.path.normcase(str(i.json_path)) for i in self.report.items},
                taken_srts={os.path.normcase(str(i.srt)) for i in self.report.items},
                first_index=self._next_index)
            for it in items:
                self._next_index = it.index + 1
                self.report.items.append(it)
                self._admit(it)
                touched.append(it)
        return touched

    def remove(self, index: int) -> BatchItem | None:
        """Take a file that has not started yet out of the queue."""
        it = next((i for i in self.report.items if i.index == index), None)
        if it is None or it.status != ST_PENDING or self._closed:
            return None
        it.status = ST_REMOVED          # the stage queues skip it
        self.report.items.remove(it)
        self._finish_one()
        return it

    async def run(self) -> BatchReport:
        if self._outstanding == 0:
            self._closed = True
            return self.report
        workers = [asyncio.ensure_future(self._prepare_worker())]
        workers += [asyncio.ensure_future(self._upload_worker())
                    for _ in range(self.jobs)]
        workers += [asyncio.ensure_future(self._segment_worker())
                    for _ in range(self.llm_jobs)]
        waiter = asyncio.ensure_future(self._done.wait())
        try:
            done, _ = await asyncio.wait(
                [waiter, *workers], return_when=asyncio.FIRST_COMPLETED)
            if waiter not in done:
                # a worker died: a bug, never a file's failure (those are
                # caught per file) -- surface it instead of hanging forever
                crashed = next(t for t in workers if t in done)
                raise crashed.exception() or RuntimeError("batch worker stopped")
        except BaseException as e:
            if isinstance(e, asyncio.CancelledError):
                self.report.interrupted = True
            for it in self.report.items:
                if it.status in (ST_RUNNING, ST_PENDING):
                    it.status = ST_CANCELLED
                    it.phase = ""
            raise
        finally:
            self._closed = True
            waiter.cancel()
            for t in workers:
                t.cancel()
            await asyncio.gather(waiter, *workers, return_exceptions=True)
            while not self._upload_q.empty():
                _it, prepared = self._upload_q.get_nowait()
                prepared.cleanup()
            try:
                await self._stack.aclose()
            except Exception as e:  # noqa: BLE001 -- closing the browser
                log.warning("closing the batch browser session failed: %s", e)
        return self.report

    # ------------------------------------------------------------ bookkeeping

    def _admit(self, it: BatchItem) -> None:
        it.phase = ""
        if it.action == ACT_SKIP:
            it.status = ST_SKIPPED
            it.detail = it.reason
            return
        it.status = ST_PENDING
        self._outstanding += 1
        if it.action == ACT_RESEGMENT:
            self._seg_q.put_nowait((it, None))
        else:
            self._prep_q.put_nowait(it)

    def _requeue(self, it: BatchItem, overwrite: bool) -> None:
        project = it.json_path if it.json_path.exists() else None
        it.action, it.reason = _action_for(project, it.srt, overwrite)
        if it.action == ACT_SKIP and it.status in (ST_FAILED, ST_CANCELLED):
            # a forced re-transcription that failed before replacing the
            # transcript leaves the old one looking done: the retry still
            # owes the upload (a newer transcript re-segments instead)
            it.action, it.reason = ACT_TRANSCRIBE, "retry"
        it.detail, it.llm_note = "", None
        it.words = it.entries = it.dialogue = 0
        it.seconds = 0.0
        self._admit(it)

    def _total(self) -> int:
        return len(self.report.items)

    def _start(self, it: BatchItem) -> None:
        it.status = ST_RUNNING
        it.started_at = time.monotonic()
        self.on_item_start(it.index, self._total(), it)

    def _set_phase(self, it: BatchItem, phase: str) -> None:
        it.phase = phase
        self.on_item_phase(it)

    def _settle(self, it: BatchItem) -> None:
        it.phase = ""
        if it.started_at:
            it.seconds = time.monotonic() - it.started_at
        self.on_item_done(it.index, self._total(), it)
        self._finish_one()

    def _finish_one(self) -> None:
        self._outstanding -= 1
        if self._outstanding <= 0:
            self._closed = True
            self._done.set()

    def _fail(self, it: BatchItem, e: BaseException) -> None:
        it.status = ST_FAILED
        it.detail = str(e) or type(e).__name__
        self._settle(it)

    def _cancel(self, it: BatchItem, why: str) -> None:
        it.status = ST_CANCELLED
        it.detail = why
        self._settle(it)

    def _succeed(self, it: BatchItem, res) -> None:
        it.status = ST_DONE
        it.words, it.entries = res.words, res.entries
        it.dialogue, it.llm_note = res.dialogue, res.llm_note
        try:
            self.on_result(it, res)
        except Exception as e:  # noqa: BLE001
            log.warning("post-processing %s failed: %s", it.audio.name, e)
        self._settle(it)

    def _abort(self, msg: str) -> None:
        """A global problem: nothing more goes to the playground.

        Files waiting for preparation or upload are cancelled; files already
        uploaded keep going through segmentation, so their subtitles (and
        the upload they cost) are not thrown away.
        """
        if self.report.aborted is None:
            self.report.aborted = msg
        self._stop_uploads = True
        while not self._prep_q.empty():
            it = self._prep_q.get_nowait()
            if it.status == ST_PENDING:
                self._cancel(it, "batch aborted")
        while not self._upload_q.empty():
            it, prepared = self._upload_q.get_nowait()
            prepared.cleanup()
            self._slots.release()
            self._cancel(it, "batch aborted")

    def _cbs(self, it: BatchItem) -> dict:
        return {"on_stage": lambda s, d: self.on_stage(it, s, d),
                "on_log": lambda m: self.on_log(it, m)}

    async def _get_page(self):
        """The shared playground page, opened on first use.

        Lazily: a queue that only re-segments never starts a browser, and one
        that gains an upload later starts it then.
        """
        async with self._page_lock:
            if self._stop_uploads:
                raise BatchAborted(self.report.aborted or "batch aborted")
            if self._page is None:
                from . import runner
                try:
                    self._page = await self._stack.enter_async_context(
                        runner.browser_session(
                            self.cfg, lambda m: self.on_log(_BATCH_ITEM, m)))
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 -- not signed in, no
                    # browser, no display: every upload would fail the same way
                    raise BatchAborted(str(e) or type(e).__name__) from e
            return self._page

    # ----------------------------------------------------------------- stages

    async def _prepare_worker(self) -> None:
        from . import runner
        while True:
            it = await self._prep_q.get()
            if it.status != ST_PENDING:
                continue                    # removed while it waited
            await self._slots.acquire()
            if it.status != ST_PENDING or self._stop_uploads:
                self._slots.release()
                if it.status == ST_PENDING:
                    self._cancel(it, "batch aborted")
                continue
            self._start(it)
            self._set_phase(it, PH_PREPARE)
            try:
                prepared = await runner.prepare_media(self.cfg, it.audio,
                                                      **self._cbs(it))
            except asyncio.CancelledError:
                self._slots.release()
                raise
            except Exception as e:  # noqa: BLE001 -- bad media is one file's failure
                self._slots.release()
                self._fail(it, e)
                continue
            if self._stop_uploads:
                prepared.cleanup()
                self._slots.release()
                self._cancel(it, "batch aborted")
                continue
            self._set_phase(it, PH_WAIT_UPLOAD)
            self._upload_q.put_nowait((it, prepared))

    async def _upload_worker(self) -> None:
        from . import runner
        from .runner import AuthRequired
        from .transcribe import browser
        while True:
            it, prepared = await self._upload_q.get()
            try:
                if self._stop_uploads:
                    self._cancel(it, "batch aborted")
                    continue
                self._set_phase(it, PH_UPLOAD)
                page = None
                try:
                    page = await self._get_page()
                    transcribed = await runner.transcribe_prepared(
                        self.cfg, prepared, it.json_path, keep=self.keep,
                        include_raw=self.include_raw,
                        on_consent=self.on_consent, page=page, **self._cbs(it))
                except asyncio.CancelledError:
                    raise
                except BatchAborted as e:
                    self._abort(str(e))
                    self._cancel(it, "batch aborted")
                    continue
                except (BiometricConsentRequired, AuthRequired, NoDisplayError) as e:
                    self._fail(it, e)
                    self._abort(str(e))
                    continue
                except Exception as e:  # noqa: BLE001 -- one file's failure, whatever its type
                    self._fail(it, e)
                    # a per-file failure that is really "the session died":
                    # probe it (cheap) so the rest does not fail 40 times over
                    if page is not None and await _session_dead(page, self.cfg, browser):
                        self._abort(SESSION_DEAD_MSG)
                    continue
            finally:
                prepared.cleanup()
                self._slots.release()
            self._set_phase(it, PH_WAIT_SEGMENT)
            self._seg_q.put_nowait((it, transcribed))

    async def _segment_worker(self) -> None:
        from . import runner
        while True:
            it, transcribed = await self._seg_q.get()
            if it.status not in (ST_PENDING, ST_RUNNING):
                continue                    # removed while it waited
            if it.status == ST_PENDING:
                self._start(it)             # a re-segment-only file
            self._set_phase(it, PH_SEGMENT)
            kw = dict(segment_params=self.segment_params,
                      post_params=self.post_params, use_llm=self.use_llm,
                      **self._cbs(it))
            try:
                if transcribed is None:
                    res = await runner.resegment_existing(
                        self.cfg, it.json_path, it.srt, **kw)
                else:
                    res = await runner.segment_transcribed(
                        self.cfg, transcribed, it.srt, **kw)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._fail(it, e)
                continue
            self._succeed(it, res)


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
    llm_jobs: int = 1,
    on_item_start: ItemCb = _noop,
    on_item_done: ItemCb = _noop,
    on_item_phase: PhaseCb = _noop,
    on_stage: ItemStageCb = _noop,
    on_log: ItemLogCb = _noop,
    on_result: ResultCb = _noop,
    on_consent=None,
) -> BatchReport:
    """Execute ``report.items`` in place (a fixed list); returns ``report``.

    ``jobs``: parallel uploads (capped at MAX_JOBS); ``llm_jobs``: files
    segmented at the same time (capped at MAX_LLM_JOBS).
    """
    return await BatchQueue(
        cfg, report, keep=keep, include_raw=include_raw,
        segment_params=segment_params, post_params=post_params,
        use_llm=use_llm, jobs=jobs, llm_jobs=llm_jobs,
        on_item_start=on_item_start, on_item_done=on_item_done,
        on_item_phase=on_item_phase, on_stage=on_stage, on_log=on_log,
        on_result=on_result, on_consent=on_consent).run()


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
