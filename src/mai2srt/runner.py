"""Shared run pipeline used by both the CLI and the UI server.

Everything here is callback-driven (``on_stage`` / ``on_log``) so the CLI can
wire it to logging while the server bridges the same callbacks to SSE events.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config, load_app_config, llm_config_from
from .media.chunk import prepare
from .media.probe import MediaError, probe
from .postprocess import PostprocessParams, process as postprocess
from .segment import SegmentParams, segment_transcript
from .segment.llm import OpenAICompatibleSplitter
from .srt import to_srt
from .transcribe import browser
from .transcribe.client import (
    BiometricConsentRequired, TranscribeError, transcribe_file,
)
from .transcribe.parser import has_word_timestamps, parse_rich
from .transcribe.stitch import stitch

log = logging.getLogger("mai2srt")

#: stage names surfaced through on_stage (UI timeline + SSE)
STAGES = ("prepare", "upload", "transcribe", "segment", "postprocess")


@dataclass
class RunResult:
    doc: dict                 # mai.json document
    srt: str                  # final subtitles
    json_path: Path
    srt_path: Path
    words: int
    entries: int
    dialogue: int
    # M6d refine hand-off: the actual SubEntry list + the sorted word refs it
    # indexes into, so the server can persist the transcription-time
    # segmentation as an origin=llm edit record without re-deriving it
    entry_list: list | None = None
    word_refs: list | None = None
    #: None = the LLM was not requested or did its job; otherwise the reason
    #: some or all over-limit runs fell back to the deterministic split (see
    #: build_entries). Surfaced to the UI so a silent quality loss is visible.
    llm_note: str | None = None


StageCb = Callable[[str, str], None]     # (stage, detail)
LogCb = Callable[[str], None]
#: async () -> bool, asked when the 451 biometric gate rejects an upload;
#: True = the user agreed to accept the playground biometric notice
ConsentCb = Callable[[], object]


def _noop_stage(stage: str, detail: str) -> None:
    pass


def _default_log(msg: str) -> None:
    log.info("%s", msg)


class AuthRequired(TranscribeError):
    """The playground session is gone and silent SSO could not restore it.

    A global condition: every further upload would fail the same way, so a
    batch aborts on it instead of failing file after file.
    """


NOT_SIGNED_IN_MSG = "登录失效：请先在设置页重新登录 (session expired; sign in first)"


@asynccontextmanager
async def browser_session(cfg: Config, on_log: LogCb = _default_log):
    """A headless, authenticated playground page, closed on exit.

    transcribe_audio opens one per call; a batch opens ONE and shares it, so
    the browser start-up and the sign-in probe are paid once, not per file.
    headless: transcription must never pop a browser window at the user; auth
    leans on the cookie jar + silent SSO only.
    """
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        ctx = await browser.launch(p, cfg, headless=True)
        try:
            page = await browser.open_page(ctx)
            status = await browser.ensure_auth(page, cfg, interactive=False)
            if status != 200:
                on_log(NOT_SIGNED_IN_MSG)
                raise AuthRequired("not signed in")
            yield page
        finally:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass


@dataclass
class PreparedMedia:
    """A source probed and cut into upload pieces (first pipeline stage).

    ``workdir`` holds the encoded pieces; whoever consumes this object owns
    it and must call :meth:`cleanup` once the pieces are uploaded or dropped.
    """
    audio: Path
    info: object                         # media.probe.MediaInfo
    workdir: Path
    pieces: list                         # [(piece path, offset seconds)]

    def cleanup(self) -> None:
        import shutil
        shutil.rmtree(self.workdir, ignore_errors=True)


@dataclass
class Transcribed:
    """An uploaded, stitched transcript already written to ``json_path``
    (second pipeline stage); segmentation is all that is left."""
    audio: Path
    doc: dict
    stitched: object                     # transcribe.stitch.StitchedTranscript
    json_path: Path


async def prepare_media(
    cfg: Config,
    audio: Path,
    on_stage: StageCb = _noop_stage,
    on_log: LogCb = _default_log,
) -> PreparedMedia:
    """Probe + compress/chunk ``audio`` into a private work directory.

    Local CPU/disk work only (ffprobe, ffmpeg): a batch runs it for the next
    file while the current one is still uploading.
    """
    if not audio.exists():
        raise TranscribeError(f"audio not found: {audio}")
    on_stage("prepare", audio.name)
    # ffprobe/ffmpeg are blocking subprocesses: offloading keeps the server
    # event loop alive so SSE progress reaches the UI DURING preparation
    info = await asyncio.to_thread(probe, audio)
    on_log("source: %s  %.1fs  %dch  %s  %.1f MiB" % (
        audio.name, info.duration_s, info.channels, info.codec,
        info.size_bytes / 1024 / 1024))

    import tempfile
    workdir = Path(tempfile.mkdtemp(prefix="mai2srt_", dir=str(cfg.data_dir)))
    prepared = PreparedMedia(audio=audio, info=info, workdir=workdir, pieces=[])
    try:
        # prepare()'s ffmpeg calls block for minutes on long video; both
        # callbacks are thread-safe (server jobs emit marshals onto the
        # loop, CLI logging is thread-safe), so progress can flow from the
        # to_thread worker directly -- without it the UI sat on a static
        # "prepare" stage and looked wedged (2026-10-13)
        def _prep_progress(msg: str) -> None:
            on_log(msg)
            on_stage("prepare", msg)
        prepared.pieces = await asyncio.to_thread(prepare, audio, info, workdir,
                                                  _prep_progress)
        on_log("upload plan: %d file(s)" % len(prepared.pieces))
    except MediaError as e:
        on_log("media preparation failed: %s" % e)
        prepared.cleanup()
        raise
    except BaseException:
        prepared.cleanup()
        raise
    return prepared


async def upload_prepared(
    cfg: Config,
    prepared: PreparedMedia,
    diarize: bool = True,
    keep: bool = False,
    include_raw: bool = False,
    on_stage: StageCb = _noop_stage,
    on_log: LogCb = _default_log,
    on_consent: ConsentCb | None = None,
    page=None,
) -> tuple[dict, object]:
    """Upload every prepared piece and stitch them -> (doc, stitched).

    ``page``: an already-authenticated playground page (see browser_session)
    to upload through; None opens, and afterwards closes, a private one.
    Does not clean the work directory up: the caller owns ``prepared``.
    """
    parts: list = []
    raws: list = []
    audio = prepared.audio
    if page is not None:
        await _upload_pieces(page, cfg, audio, prepared.pieces, diarize, keep,
                             parts, raws, on_stage, on_log, on_consent)
    else:
        async with browser_session(cfg, on_log) as own_page:
            await _upload_pieces(own_page, cfg, audio, prepared.pieces,
                                 diarize, keep, parts, raws, on_stage, on_log,
                                 on_consent)
    stitched = stitch(parts, prepared.info.duration_s)
    doc = build_document(audio, stitched, raws, include_raw)
    return doc, stitched


async def transcribe_audio(
    cfg: Config,
    audio: Path,
    diarize: bool = True,
    keep: bool = False,
    include_raw: bool = False,
    on_stage: StageCb = _noop_stage,
    on_log: LogCb = _default_log,
    on_consent: ConsentCb | None = None,
    page=None,
) -> tuple[dict, object]:
    """Probe -> prepare -> upload per chunk -> stitch. Returns (doc, stitched).

    ``page``: an already-authenticated playground page (see browser_session)
    to upload through; None opens, and afterwards closes, a private one.
    """
    prepared = await prepare_media(cfg, audio, on_stage, on_log)
    try:
        return await upload_prepared(
            cfg, prepared, diarize=diarize, keep=keep, include_raw=include_raw,
            on_stage=on_stage, on_log=on_log, on_consent=on_consent, page=page)
    finally:
        await asyncio.to_thread(prepared.cleanup)


async def transcribe_prepared(
    cfg: Config,
    prepared: PreparedMedia,
    json_path: Path,
    diarize: bool = True,
    keep: bool = False,
    include_raw: bool = False,
    on_stage: StageCb = _noop_stage,
    on_log: LogCb = _default_log,
    on_consent: ConsentCb | None = None,
    page=None,
) -> Transcribed:
    """Upload a prepared source and write its transcript (.mai.json).

    The transcript is on disk before segmentation starts, so an LLM failure
    or a cancel later on never costs the (slow, quota-consuming) upload: the
    next run only re-segments.
    """
    doc, stitched = await upload_prepared(
        cfg, prepared, diarize=diarize, keep=keep, include_raw=include_raw,
        on_stage=on_stage, on_log=on_log, on_consent=on_consent, page=page)
    write_transcript(doc, json_path)
    on_log("wrote %s (%d words)" % (json_path, len(stitched.transcript.words)))
    return Transcribed(audio=prepared.audio, doc=doc, stitched=stitched,
                       json_path=json_path)


def write_transcript(doc: dict, json_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


async def _upload_pieces(page, cfg, audio, pieces, diarize, keep, parts, raws,
                         on_stage, on_log, on_consent) -> None:
    """Upload each prepared piece through ``page``; appends to parts/raws."""
    for i, (upath, offset) in enumerate(pieces):
        on_stage("upload", "chunk %d/%d" % (i + 1, len(pieces)))
        try:
            res = await transcribe_file(
                page, upath, _mime(upath),
                diarize=diarize,
                keep=keep,
                title="mai2srt %s %s" % (audio.stem[:30],
                                         time.strftime("%m%d_%H%M")),
                reauth=lambda: browser.ensure_auth(page, cfg, interactive=False),
                consent=on_consent,
            )
        except BiometricConsentRequired:
            # the GUI has no prompt here (on_consent is None): the
            # error text itself carries the settings-page guidance;
            # the log line below makes it visible in the job log too
            on_log("需要先接受一次生物特征通知（设置页 → 「接受生物特征通知」，"
                   "或 CLI: mai2srt consent），然后重试转录")
            raise
        raws.append(res.rich)
        if res.cleanup_ok is False:
            # "leave no trace" failed: the promise is user-facing, so
            # the failure must be too (file-log-only is not enough)
            on_log("会话清理删除失败：该转录记录仍留在 playground 上，"
                   "可稍后在网页手动删除 (conversation cleanup failed)")
        on_stage("transcribe", "chunk %d" % (i + 1))
        t = parse_rich(res.rich)
        if not has_word_timestamps(t):
            on_log("chunk %d returned no word timestamps; aborting" % i)
            raise TranscribeError(f"chunk {i}: no word timestamps")
        parts.append((t, offset, upath.name))
        on_log("chunk %d done: %d utts / %d words" % (
            i, len(t.utterances), len(t.words)))


def build_document(source: Path, stitched, raws: list, include_raw: bool) -> dict:
    """Serialize a stitched transcript into the .mai.json document shape."""
    t = stitched.transcript
    doc = {
        "text": t.text(),
        "language": t.language,
        "duration": stitched.audio_duration_s,
        "engine": "playground_mai-transcribe-2",
        "source": str(source),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "chunks": [
            {"file": c.file, "offset_s": round(c.offset_s, 3),
             "duration_s": round(c.duration_s, 3),
             "utterances": c.utterances, "words": c.words}
            for c in stitched.chunks
        ],
        "utterances": [
            {"speaker": u.speaker, "start": round(u.start, 3),
             "end": round(u.end, 3), "text": u.text, "language": u.language}
            for u in t.utterances
        ],
        "words": [
            {"text": w.text, "start": round(w.start, 3),
             "end": round(w.end, 3), "speaker": w.speaker}
            for w in t.words
        ],
    }
    if include_raw:
        doc["raw_responses"] = raws
    return doc


def _mime(path: Path) -> str:
    ext = path.suffix.lstrip(".").lower()
    if ext == "mp3":
        return "audio/mpeg"
    if ext == "wav":
        return "audio/wav"
    raise MediaError(f"prepared file has unexpected extension: {path.name}")


def build_entries(
    cfg: Config,
    transcript,
    audio_duration_s: float,
    segment_params: SegmentParams | None = None,
    post_params: PostprocessParams | None = None,
    use_llm: bool = True,
    on_stage: StageCb = _noop_stage,
    on_log: LogCb = _default_log,
) -> tuple[list, str | None]:
    """Segment + post-process -> (SubEntry list, llm_note).

    The refine workspace needs the entries themselves (each carries its word
    list, so manual merge/split/text edits can recompute timestamps); the SRT
    text is only the final serialization of that structure.

    ``llm_note`` is None when the LLM was not requested or worked; the string
    "unavailable" when no endpoint is configured; otherwise the splitter's
    failure message (results then came from the deterministic fallback).
    """
    sp = segment_params or SegmentParams()
    pp = post_params or PostprocessParams(
        max_duration_s=sp.max_duration_s, max_chars=sp.max_chars,
        min_duration_s=sp.min_duration_s,
    )
    splitter = None
    if use_llm:
        splitter = _make_splitter(cfg, on_log)
    on_stage("segment", "")
    llm_err: list[str] = []
    segs = segment_transcript(transcript, sp, splitter=splitter, log=on_log,
                              llm_status=llm_err)
    on_log("segmented: %d lines" % len(segs))
    on_stage("postprocess", "")
    entries = postprocess(segs, pp, audio_duration_s=audio_duration_s,
                          split_params=sp, log=on_log)
    dial = sum(1 for e in entries if e.is_dialogue)
    on_log("post-processed: %d entries (%d dialogue)" % (len(entries), dial))
    llm_note: str | None = None
    if use_llm:
        if splitter is None:
            llm_note = "unavailable"
        elif llm_err:
            llm_note = llm_err[0]
    return entries, llm_note


def finish_srt(
    cfg: Config,
    transcript,
    audio_duration_s: float,
    segment_params: SegmentParams | None = None,
    post_params: PostprocessParams | None = None,
    use_llm: bool = True,
    on_stage: StageCb = _noop_stage,
    on_log: LogCb = _default_log,
) -> tuple[str, int, int]:
    """Segment + post-process -> (srt_text, entry_count, dialogue_count)."""
    entries, _llm_note = build_entries(
        cfg, transcript, audio_duration_s,
        segment_params=segment_params, post_params=post_params,
        use_llm=use_llm, on_stage=on_stage, on_log=on_log)
    dial = sum(1 for e in entries if e.is_dialogue)
    return to_srt(entries), len(entries), dial


def _make_splitter(cfg: Config, on_log: LogCb = _default_log):
    """Active-endpoint splitter: llm_active (multi-provider) > legacy llm."""
    from .segment.llm import ProtocolSplitter, resolve_endpoint
    app_config = load_app_config(cfg)
    endpoint = resolve_endpoint(app_config)
    if not endpoint.api_key or not endpoint.model:
        on_log("no LLM endpoint configured; using deterministic punctuation split")
        return None
    on_log("LLM split-point selection: %s @ %s (%s, effort=%s)" % (
        endpoint.model, endpoint.base_url, endpoint.protocol,
        endpoint.effort or "n/a"))
    return ProtocolSplitter(endpoint, log=on_log)


async def run_full(
    cfg: Config,
    audio: Path,
    json_path: Path | None = None,
    diarize: bool = True,
    keep: bool = False,
    include_raw: bool = False,
    segment_params: SegmentParams | None = None,
    post_params: PostprocessParams | None = None,
    use_llm: bool = True,
    on_stage: StageCb = _noop_stage,
    on_log: LogCb = _default_log,
    on_consent: ConsentCb | None = None,
    page=None,
    srt_path: Path | None = None,
) -> RunResult:
    """transcribe + segment + postprocess in one shot; writes both artifacts.

    json_path: where the .mai.json goes (None = next to the audio, the CLI
    default; the GUI passes the storage-library path). The .srt lands next
    to the audio unless ``srt_path`` says otherwise (batch --out-dir) --
    subtitles follow the media, transcripts follow the storage policy.
    ``page``: a shared authenticated page (batch); None = private browser."""
    prepared = await prepare_media(cfg, audio, on_stage, on_log)
    try:
        transcribed = await transcribe_prepared(
            cfg, prepared, json_path or audio.with_suffix(".mai.json"),
            diarize=diarize, keep=keep, include_raw=include_raw,
            on_stage=on_stage, on_log=on_log, on_consent=on_consent, page=page)
    finally:
        await asyncio.to_thread(prepared.cleanup)
    return await segment_transcribed(
        cfg, transcribed, srt_path or audio.parent / (audio.stem + ".srt"),
        segment_params=segment_params, post_params=post_params,
        use_llm=use_llm, on_stage=on_stage, on_log=on_log)


async def segment_transcribed(
    cfg: Config,
    transcribed: Transcribed,
    srt_path: Path,
    segment_params: SegmentParams | None = None,
    post_params: PostprocessParams | None = None,
    use_llm: bool = True,
    on_stage: StageCb = _noop_stage,
    on_log: LogCb = _default_log,
) -> RunResult:
    """Last pipeline stage: segment a fresh transcript and write the SRT."""
    st = transcribed.stitched
    return await _segment_and_write(
        cfg, transcribed.doc, st.transcript, st.audio_duration_s,
        transcribed.json_path, srt_path, segment_params, post_params,
        use_llm, on_stage, on_log)


async def resegment_existing(
    cfg: Config,
    json_path: Path,
    srt_path: Path,
    segment_params: SegmentParams | None = None,
    post_params: PostprocessParams | None = None,
    use_llm: bool = True,
    on_stage: StageCb = _noop_stage,
    on_log: LogCb = _default_log,
) -> RunResult:
    """Rebuild the SRT from a transcript that is already on disk.

    The resume path of a batch: the (slow, quota-consuming) upload finished
    in an earlier run and only segmentation/export did not. No browser, no
    network apart from the optional LLM split calls.
    """
    from .transcribe.persist import load_mai_json
    t = await asyncio.to_thread(load_mai_json, json_path)
    if not t.words:
        raise TranscribeError("no words in %s; re-transcribe the source" % json_path)
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    on_log("reusing transcript %s (%d words)" % (json_path.name, len(t.words)))
    return await _segment_and_write(
        cfg, doc, t, t.duration_s, json_path, srt_path, segment_params,
        post_params, use_llm, on_stage, on_log)


async def _segment_and_write(cfg, doc, transcript, duration_s, json_path,
                             srt_path, segment_params, post_params, use_llm,
                             on_stage, on_log) -> RunResult:
    # segmentation + LLM split-point calls are blocking (sync HTTP): offload.
    # build_entries (not finish_srt) so the entry structure survives for the
    # refine hand-off edit record
    entries, llm_note = await asyncio.to_thread(
        build_entries, cfg, transcript, duration_s, segment_params,
        post_params, use_llm, on_stage, on_log)
    n_dial = sum(1 for e in entries if e.is_dialogue)
    srt = to_srt(entries)
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.write_text(srt, encoding="utf-8")
    on_log("wrote %s" % srt_path)
    if llm_note:
        on_log("大模型断句未完全生效：%s" % llm_note)
    return RunResult(
        doc=doc, srt=srt, json_path=json_path, srt_path=srt_path,
        words=len(transcript.words), entries=len(entries), dialogue=n_dial,
        entry_list=entries,
        word_refs=sorted(transcript.words, key=lambda w: w.start),
        llm_note=llm_note)


async def consent_flow(cfg: Config, on_log: LogCb = _default_log) -> dict:
    """Accept the playground biometric notice once for the active account.

    Same POST the site's own consent dialog makes (issue #1: audio counts
    as biometric data under BIPA, and a headless client never sees the
    dialog -- uploads hard-fail with 451 until the account has accepted).
    The acceptance is stored server-side, so it survives this session.
    Idempotent: reports "already accepted" when the account needs nothing.
    """
    from playwright.async_api import async_playwright
    from .transcribe.client import (
        CONSENT_KIND, accept_biometric_consent, consent_status,
    )
    async with async_playwright() as p:
        ctx = await browser.launch(p, cfg, headless=True)
        try:
            page = await browser.open_page(ctx)
            status = await browser.ensure_auth(page, cfg, interactive=False)
            if status != 200:
                on_log("登录失效：请先在设置页重新登录 (session expired; sign in first)")
                raise TranscribeError("not signed in")
            st = await consent_status(page, CONSENT_KIND)
            if not st.get("needed"):
                on_log("账号无需接受生物特征通知 (no biometric consent needed)")
                return {"accepted": False, "needed": False, "status": st}
            on_log("playground 要求接受生物特征通知（音频按生物特征数据处理，"
                   "辖区 %s）" % st.get("jurisdiction", "?"))
            res = await accept_biometric_consent(page, CONSENT_KIND)
            on_log("已接受生物特征通知，上传已解锁 (biometric notice accepted)")
            return {"accepted": True, "needed": True, "status": res}
        finally:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass


async def login_flow(cfg: Config, on_log: LogCb = _default_log,
                     consent: ConsentCb | None = None) -> dict:
    """Interactive browser sign-in; returns {"status", "consent_needed",
    "consent_accepted"}.

    The visible browser opens DIRECTLY on the Microsoft account picker --
    no playground chat page flash before the login UI. Raises when the
    sign-in does not complete (window closed / timeout) so the UI job
    ends in an error state instead of a false success.

    A fresh account always owes the playground biometric notice (issue
    #1) and the user is right here with a live session: the still-open
    login page checks the notice (one GET, no side effects) and, when
    ``consent`` returns True, accepts it NOW -- instead of letting the
    first transcription discover the 451. Non-interactive callers pass
    consent=None and surface "consent_needed" themselves.
    """
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        ctx = await browser.launch(p, cfg, headless=False)
        page = await browser.open_page(ctx, url=browser.login_url("select_account"))
        status = await browser.wait_for_login(page, cfg)
        consent_needed = False
        consent_accepted = False
        if status == 200:
            try:
                from .transcribe.client import (
                    CONSENT_KIND, accept_biometric_consent, consent_status,
                )
                st = await consent_status(page, CONSENT_KIND)
                consent_needed = bool(st.get("needed"))
                if consent_needed:
                    on_log("检测到该账号尚未接受生物特征通知（音频按生物特征数据"
                           "处理，BIPA），首次转录前需要接受一次")
                    if consent is not None and await consent():
                        await accept_biometric_consent(page, CONSENT_KIND)
                        consent_accepted = True
                        consent_needed = False
                        on_log("已接受生物特征通知，上传已解锁 "
                               "(biometric notice accepted)")
                    elif consent is None:
                        on_log("先在设置页点「接受生物特征通知」再开始转录 "
                               "(Settings → accept biometric notice)")
                else:
                    on_log("账号无需接受生物特征通知 (no biometric consent needed)")
            except TranscribeError as e:
                # the login itself succeeded; a consent hiccup must never
                # turn that into a failed job
                on_log("生物特征通知状态查询失败（不影响登录）：%s" % e)
        try:
            await ctx.close()
        except Exception:  # noqa: BLE001
            pass
    on_log("auth status: %s" % status)
    if status != 200:
        raise TranscribeError(
            "登录未完成：浏览器窗口被关闭或等待超时 (sign-in not completed)")
    return {"status": status, "consent_needed": consent_needed,
            "consent_accepted": consent_accepted}
