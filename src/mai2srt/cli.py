"""mai2srt command line: login / consent / transcribe / process / run.

    mai2srt login
    mai2srt session export <file> | import <file>   # move a login between machines
    mai2srt consent             # accept the playground biometric notice once
    mai2srt transcribe <audio> [--out out.mai.json]
                        [--keep-conversation] [--include-raw]
    mai2srt process <x.mai.json> [--out out.srt] [--no-llm] [tuning flags]
    mai2srt run <audio> [--out out.srt] [--json out.mai.json] [--no-llm]
              [tuning flags]
    mai2srt batch <file|dir>... [-r] [--out-dir D] [--overwrite] [--dry-run]
              [--jobs N] [--llm-jobs N] [--report r.json] [--no-llm]
              [tuning flags]

Speaker diarization cannot be disabled: the playground server ignores
{"diarize": false} entirely (live-verified 2026-09-26 -- an empty
transcribeOptions AND an explicit false both came back diarized), so
the old --no-diarize flag was a no-op and has been removed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

from . import __version__
from .config import Config
from .media.probe import MediaError
from .runner import (
    build_document,
    finish_srt,
    login_flow,
    run_full,
    transcribe_audio,
)
from .transcribe.browser import NoDisplayError
from .transcribe.client import TranscribeError
from .transcribe.persist import load_mai_json

log = logging.getLogger("mai2srt")


def _setup_logging(cfg: Config, verbose: bool) -> None:
    cfg.ensure_dirs()
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                            "%H:%M:%S")
    root = logging.getLogger("mai2srt")
    root.setLevel(level)
    root.handlers.clear()
    con = logging.StreamHandler(sys.stderr)
    con.setFormatter(fmt)
    root.addHandler(con)
    fh = logging.FileHandler(cfg.log_dir / "mai2srt.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


async def cmd_login(cfg: Config, account: str | None = None) -> int:
    if account:
        from .config import add_account, set_active_account
        name = account.strip()
        try:
            add_account(cfg, name)
        except ValueError:
            pass  # already registered (or default): just (re)sign in
        set_active_account(cfg, name)
        log.info("signing in account '%s'", name)
    try:
        await login_flow(cfg, on_log=log.info, consent=_consent_prompt)
    except (TranscribeError, NoDisplayError) as e:
        log.error("%s", e)
        return 1
    log.info("signed in; session persisted under %s", cfg.data_dir)
    return 0


def _resolve_keep(cfg: Config, args: argparse.Namespace) -> bool:
    """--keep-conversation overrides the app-config toggle; when the flag is
    absent the toggle (default ON = delete) decides, so CLI and GUI agree."""
    if args.keep_conversation:
        return True
    from .config import auto_delete_conversation
    return not auto_delete_conversation(cfg)


async def _consent_prompt() -> bool:
    """Terminal Y/N when the 451 biometric gate rejects an upload.

    A legal notice is accepted only by an explicit user action: the CLI
    asks (the website shows the same dialog) instead of silently POSTing
    the acceptance. Non-interactive stdin (pipes/scripts) declines, so
    the run ends with the `mai2srt consent` guidance instead of hanging.
    """
    if not sys.stdin.isatty():
        return False
    print("\nplayground 要求当前账号先接受一次生物特征通知（音频按生物特征数据"
          "处理，BIPA）后才能上传：\n"
          "接受后记录保存在账号上，仅此一次。是否现在接受？ [y/N] ",
          end="", flush=True)
    answer = await asyncio.to_thread(input)
    return answer.strip().lower() in ("y", "yes")


def cmd_session(cfg: Config, args: argparse.Namespace) -> int:
    from .session_io import SessionIOError, export_session, import_session
    try:
        if args.action == "export":
            name = export_session(cfg, Path(args.file), account=args.account)
            log.info("exported account '%s' -> %s", name, args.file)
            log.warning("this file is a login credential (anyone holding it can "
                        "use the account): keep it private and delete it after "
                        "importing")
        else:
            name = import_session(cfg, Path(args.file), account=args.account)
            log.info("imported account '%s'; it is now the active account", name)
    except SessionIOError as e:
        log.error("%s", e)
        return 1
    return 0


async def cmd_consent(cfg: Config) -> int:
    from .runner import consent_flow
    try:
        await consent_flow(cfg, on_log=log.info)
    except TranscribeError as e:
        log.error("%s", e)
        return 1
    return 0


async def cmd_transcribe(cfg: Config, args: argparse.Namespace) -> int:
    audio = Path(args.audio)
    doc, stitched = await transcribe_audio(
        cfg, audio,
        keep=_resolve_keep(cfg, args),
        include_raw=args.include_raw,
        on_log=log.info,
        on_consent=_consent_prompt)
    out_path = Path(args.out) if args.out else audio.with_suffix(".mai.json")
    _write_doc(out_path, doc)
    log.info("wrote %s  (%d words, %d utterances, %.1fs)",
             out_path, len(stitched.transcript.words),
             len(stitched.transcript.utterances), stitched.audio_duration_s)
    return 0


def _write_doc(out_path: Path, doc: dict) -> None:
    out_path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------
# segmentation + post-processing (M2/M3) -- thin wrappers over runner
# ---------------------------------------------------------------------------

def _tuned_params(args: argparse.Namespace) -> tuple:
    from .segment import SegmentParams
    from .postprocess import PostprocessParams
    sp = SegmentParams(
        max_duration_s=args.max_duration,
        max_chars=args.max_chars,
        min_duration_s=args.min_duration,
        min_chars=args.min_chars,
        split_pause_s=args.split_pause,
    )
    pp = PostprocessParams(
        max_duration_s=args.max_duration,
        max_chars=args.max_chars,
        min_duration_s=args.min_duration,
        merge_gap_threshold_s=args.merge_gap,
        dialogue_gap_tolerance_s=args.tolerance,
        mai_expand_s=args.expand,
    )
    return sp, pp


def cmd_process(cfg: Config, args: argparse.Namespace) -> int:
    src = Path(args.mai_json)
    if not src.exists():
        log.error("input not found: %s", src)
        return 1
    t = load_mai_json(src)
    if not t.words:
        log.error("no words in %s; re-transcribe the source audio", src)
        return 1
    log.info("loaded %s: %d words, %.1fs", src.name, len(t.words), t.duration_s)
    sp, pp = _tuned_params(args)
    srt, n_entries, n_dial = finish_srt(
        cfg, t, t.duration_s, segment_params=sp, post_params=pp,
        use_llm=not args.no_llm, on_log=log.info)

    out = Path(args.out) if args.out else src.with_suffix(".srt")
    out.write_text(srt, encoding="utf-8")
    log.info("wrote %s  (%d entries, %d dialogue)", out, n_entries, n_dial)
    return 0


async def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    audio = Path(args.audio)
    sp, pp = _tuned_params(args)
    res = await run_full(
        cfg, audio,
        keep=_resolve_keep(cfg, args),
        include_raw=args.include_raw,
        segment_params=sp, post_params=pp,
        use_llm=not args.no_llm,
        on_log=log.info,
        on_consent=_consent_prompt)
    log.info("done: %d words -> %d entries (%d dialogue)",
             res.words, res.entries, res.dialogue)
    return 0


def cmd_batch(cfg: Config, args: argparse.Namespace) -> int:
    """Many files, one browser session; resumable (see mai2srt.batch)."""
    from . import batch as b

    files, problems = b.collect_inputs(args.inputs, recursive=args.recursive)
    for msg in problems:
        log.warning("%s", msg)
    if not files:
        log.error("no input files")
        return 2
    out_dir = Path(args.out_dir).resolve() if args.out_dir else None
    items = b.plan_batch(cfg, files, out_dir=out_dir, overwrite=args.overwrite)

    if args.dry_run:
        print(b.format_plan(items))
        return 0

    needs_upload = any(i.action == b.ACT_TRANSCRIBE for i in items)
    if needs_upload:
        # the LLM is optional here (segmentation degrades to rules), so it is
        # not a prerequisite; ffmpeg / browser / a saved login are
        from .preflight import install_hints, missing_requirements
        missing = missing_requirements(cfg, use_llm=False)
        if missing:
            hints = install_hints(missing)
            for key in missing:
                log.error("missing prerequisite: %s%s", key,
                          "  ->  " + hints[key] if key in hints else "")
            return 1

    sp, pp = _tuned_params(args)
    from .config import llm_jobs as stored_llm_jobs
    llm_jobs = args.llm_jobs if args.llm_jobs is not None else stored_llm_jobs(cfg)
    report = b.BatchReport(items, input_problems=list(problems))
    order = {id(it): n for n, it in enumerate(items, 1)}
    total = len(items)

    def started(idx: int, tot: int, it: b.BatchItem) -> None:
        log.info("[%d/%d] %s  %s", idx + 1, tot, it.action, it.audio)

    def finished(idx: int, tot: int, it: b.BatchItem) -> None:
        if it.status == b.ST_DONE:
            log.info("[%d/%d] ok  %d lines, %.0fs%s", idx + 1, tot, it.entries,
                     it.seconds, "  (LLM fell back to rules)" if it.llm_note else "")
        elif it.status == b.ST_FAILED:
            log.error("[%d/%d] FAILED  %s", idx + 1, tot,
                      it.detail.splitlines()[0][:300] if it.detail else "")

    def on_log(it: b.BatchItem, msg: str) -> None:
        n = order.get(id(it))
        log.info("%s%s", "[%d/%d] " % (n, total) if n else "", msg)

    async def go() -> None:
        await b.run_batch(
            cfg, report,
            keep=_resolve_keep(cfg, args), include_raw=args.include_raw,
            segment_params=sp, post_params=pp, use_llm=not args.no_llm,
            jobs=args.jobs, llm_jobs=llm_jobs,
            on_item_start=started, on_item_done=finished,
            on_log=on_log, on_consent=_consent_prompt)

    interrupted = False
    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        interrupted = True
        report.interrupted = True
        for it in report.items:
            if it.status in (b.ST_RUNNING, b.ST_PENDING):
                it.status = b.ST_CANCELLED

    print()
    print(b.format_summary(report))
    if args.report:
        try:
            _write_doc(Path(args.report), report.to_dict())
            log.info("report written to %s", args.report)
        except OSError as e:
            log.error("cannot write report: %s", e)
    if interrupted:
        return 130
    return 0 if report.ok else 1


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    import uvicorn
    from .server.app import create_app
    uvicorn.run(create_app(cfg), host="127.0.0.1", port=args.port,
                log_level="info")
    return 0


def _add_tuning(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--max-duration", type=float, default=12.0,
                    help="max line duration s (default 12)")
    sp.add_argument("--max-chars", type=int, default=60,
                    help="max line chars (default 60)")
    sp.add_argument("--min-duration", type=float, default=1.2,
                    help="min line duration s (default 1.2)")
    sp.add_argument("--min-chars", type=int, default=5,
                    help="min line chars, 0 disables (default 5)")
    sp.add_argument("--split-pause", type=float, default=0.8,
                    help="inter-word pause s that marks a split candidate (default 0.8)")
    sp.add_argument("--merge-gap", type=float, default=0.8,
                    help="max gap s the smart merge may bridge (default 0.8)")
    sp.add_argument("--tolerance", type=float, default=0.2,
                    help="dialogue clustering tolerance s (default 0.2)")
    sp.add_argument("--expand", type=float, default=0.25,
                    help="timestamp outward expansion s (default 0.25)")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stderr, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser(
        prog="mai2srt",
        description="playground.microsoft.ai MAI-Transcribe-2 transcription "
                    "-> word-timestamp JSON -> segmented/dialogue SRT")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("login", help="open a browser and (re)establish the session")
    lp.add_argument("--account", default=None,
                    help="sign into this named account (created if new); "
                         "each account keeps its own cookies + browser profile")

    ssp = sub.add_parser(
        "session", help="export/import the signed-in session (for headless "
                        "machines that cannot open a browser to sign in)")
    ssp.add_argument("action", choices=["export", "import"])
    ssp.add_argument("file")
    ssp.add_argument("--account", default=None,
                     help="export: which account (default active); "
                          "import: store under this name")

    cp = sub.add_parser(
        "consent", help="accept the playground biometric notice once "
                        "(unblocks audio uploads rejected with HTTP 451)")

    tp = sub.add_parser("transcribe", help="audio -> <name>.mai.json")
    tp.add_argument("audio")
    tp.add_argument("--out", default=None, help="output json path")
    tp.add_argument("--keep-conversation", action="store_true", default=None,
                    help="keep the playground conversation (overrides the "
                         "app config toggle; default = config value)")
    tp.add_argument("--include-raw", action="store_true",
                    help="embed raw rich_transcription payloads")

    pp = sub.add_parser("process", help="<name>.mai.json -> SRT (offline)")
    pp.add_argument("mai_json")
    pp.add_argument("--out", default=None, help="output srt path")
    pp.add_argument("--no-llm", action="store_true",
                    help="skip LLM split-point selection (deterministic only)")
    _add_tuning(pp)

    rp = sub.add_parser("run", help="audio -> transcribe + segment + SRT")
    rp.add_argument("audio")
    rp.add_argument("--out", default=None, help="output srt path")
    rp.add_argument("--json", default=None, help="output json path "
                    "(default <name>.mai.json next to the audio)")
    rp.add_argument("--keep-conversation", action="store_true", default=None)
    rp.add_argument("--include-raw", action="store_true")
    rp.add_argument("--no-llm", action="store_true",
                    help="skip LLM split-point selection (deterministic only)")
    _add_tuning(rp)

    bp = sub.add_parser(
        "batch", help="many audio/video files -> SRT, one browser session; "
                      "re-running skips finished files")
    bp.add_argument("inputs", nargs="+",
                    help="media files and/or folders")
    bp.add_argument("-r", "--recursive", action="store_true",
                    help="descend into subfolders of the given folders")
    bp.add_argument("--out-dir", default=None,
                    help="write every .srt here (default: next to each "
                         "source); same-named sources get a short hash suffix")
    bp.add_argument("--overwrite", action="store_true",
                    help="redo files that are already finished")
    bp.add_argument("--dry-run", action="store_true",
                    help="list what would run / be re-segmented / be skipped "
                         "and exit; uploads nothing, creates nothing")
    bp.add_argument("--jobs", type=int, default=1,
                    help="parallel uploads (default 1, max 3; the playground "
                         "is a free shared service - raise with care). The "
                         "next file is always prepared, and the previous one "
                         "segmented, while one uploads")
    bp.add_argument("--llm-jobs", type=int, default=None,
                    help="files segmented by the LLM at the same time "
                         "(default: the app setting, 1; max 4)")
    bp.add_argument("--report", default=None,
                    help="also write a machine-readable JSON report here")
    bp.add_argument("--keep-conversation", action="store_true", default=None)
    bp.add_argument("--include-raw", action="store_true")
    bp.add_argument("--no-llm", action="store_true",
                    help="skip LLM split-point selection (deterministic only)")
    _add_tuning(bp)

    sv = sub.add_parser("serve", help="run the UI backend API server")
    sv.add_argument("--port", type=int, default=47613)

    args = ap.parse_args(argv)
    cfg = Config()
    _setup_logging(cfg, args.verbose)

    if args.cmd == "login":
        return asyncio.run(cmd_login(cfg, account=args.account))
    if args.cmd == "session":
        return cmd_session(cfg, args)
    if args.cmd == "consent":
        return asyncio.run(cmd_consent(cfg))
    if args.cmd == "transcribe":
        try:
            return asyncio.run(cmd_transcribe(cfg, args))
        except (TranscribeError, MediaError) as e:
            log.error("transcription failed: %s", e)
            return 1
    if args.cmd == "process":
        return cmd_process(cfg, args)
    if args.cmd == "run":
        try:
            return asyncio.run(cmd_run(cfg, args))
        except (TranscribeError, MediaError) as e:
            log.error("run failed: %s", e)
            return 1
    if args.cmd == "batch":
        return cmd_batch(cfg, args)
    if args.cmd == "serve":
        return cmd_serve(cfg, args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
