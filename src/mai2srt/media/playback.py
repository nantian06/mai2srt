"""On-demand audio-extraction cache: video (and exotic-audio) sources
for refine-page playback.

The <audio> element only plays what the /api/audio allowlist serves, so
a source outside it (a video file, or wma/aiff/...) gets its audio track
extracted ONCE into an app-data cache; every later open is instant:
  - aac/mp3/opus/vorbis/flac tracks -> -vn -c:a copy remux (lossless,
    IO-bound: seconds for an hour of audio)
  - anything else (ac3/dts/pcm...)  -> mp3 192k transcode (~50-100x
    realtime)
  - on Linux AAC is "anything else" too: the WebKitGTK webview cannot
    decode it on a stock system (see webview_decodes_aac), so AAC tracks
    and .m4a/.aac files are transcoded there
Cache names hash (resolved path + mtime + size), so a changed source
naturally invalidates its entry; index.json maps hashes back to source
paths for the settings-card cache manager. The cache is pure derived
data -- deleting any entry only costs the next first-play extraction.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from ..config import Config
from .probe import MediaError, probe, resolve_tool, run_tool

log = logging.getLogger("mai2srt.media")

#: every audio extension the endpoint has a MIME type for, and the audio
#: files a user may bind for playback. Whether the webview can DECODE one is
#: a separate, per-platform question: see direct_play_mimes().
AUDIO_MIMES = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".flac": "audio/flac", ".opus": "audio/ogg",
}

#: the AAC carriers among AUDIO_MIMES
_AAC_EXTS = {".m4a", ".aac"}

#: video containers the transcribe page accepts (drop + file dialog)
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv",
              ".ts", ".flv"}

#: audio codecs the element decodes natively -> remux instead of
#: transcode (target extension must be directly playable and a container
#: ffmpeg can write via stream copy)
_COPY_TARGET = {"aac": ".m4a", "mp3": ".mp3", "flac": ".flac",
                "opus": ".opus", "vorbis": ".ogg"}


def webview_decodes_aac() -> bool:
    """Can the desktop app's webview decode AAC with what a stock system has?

    Windows (WebView2, Chromium) and macOS (WKWebView) can. Linux renders the
    UI with WebKitGTK, which hands media to GStreamer, and a stock install
    (gst-plugins-base + good -- also all the AppImage bundles) has NO AAC
    decoder: that needs gst-libav or plugins-bad. The pipeline then stops at
    "Missing decoder: MPEG-4 AAC" and the play button stays silent. Most
    videos carry AAC, so their remuxed .m4a cache was unplayable there; mp3,
    Opus, Vorbis, FLAC and WAV all decode with base + good.
    """
    return sys.platform in ("win32", "darwin")


def direct_play_mimes() -> dict[str, str]:
    """Extensions /api/audio serves AS-IS on this platform (with their MIME).

    Owned here so the whole playback chain -- endpoint, extraction decision,
    cache lookup -- shares one truth. Everything else goes through the
    extraction cache first.
    """
    if webview_decodes_aac():
        return dict(AUDIO_MIMES)
    return {k: v for k, v in AUDIO_MIMES.items() if k not in _AAC_EXTS}


def _copy_targets() -> dict[str, str]:
    """Codec -> remux target, limited to what this platform can decode."""
    if webview_decodes_aac():
        return dict(_COPY_TARGET)
    return {k: v for k, v in _COPY_TARGET.items() if v not in _AAC_EXTS}


def is_video(p) -> bool:
    return Path(p).suffix.lower() in VIDEO_EXTS


def is_bindable(p) -> bool:
    """May this file be bound as a transcript's playback audio?"""
    return Path(p).suffix.lower() in AUDIO_MIMES or is_video(p)


def needs_extraction(p) -> bool:
    """True when /api/audio cannot serve this file as-is."""
    return Path(p).suffix.lower() not in direct_play_mimes()


def audio_cache_dir(cfg: Config) -> Path:
    return cfg.data_dir / "audio-cache"


def cache_key(src: Path) -> str:
    st = src.stat()
    raw = "%s|%d|%d" % (src.resolve(), int(st.st_mtime), st.st_size)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def existing_cache(cfg: Config, src: Path) -> Path | None:
    """The cached playable audio for src, None when not extracted yet.
    Never probes and never extracts: cheap enough for status queries."""
    if not needs_extraction(src):
        return None
    try:
        hits = sorted(audio_cache_dir(cfg).glob(cache_key(src) + ".*"))
    except OSError:
        return None
    playable = direct_play_mimes()
    for h in hits:
        # an entry this platform cannot decode (an AAC remux made before
        # Linux stopped producing them) is not a hit: it gets re-extracted
        if ".part" not in h.name and h.suffix.lower() in playable:
            return h
    return None


# ----------------------------------------------------------------- extract

def _ffmpeg(args: list[str], dst: Path) -> None:
    cmd = [resolve_tool("ffmpeg"), "-y", "-v", "error", *args, str(dst)]
    r = run_tool(cmd, capture_output=True, text=True,
                 encoding="utf-8", errors="replace", timeout=7200)
    if r.returncode != 0 or not dst.exists():
        raise MediaError(
            f"ffmpeg audio extraction failed: {(r.stderr or '')[-300:]}")


def _index_load(cfg: Config) -> dict:
    try:
        data = json.loads(
            (audio_cache_dir(cfg) / "index.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _index_save(cfg: Config, data: dict) -> None:
    d = audio_cache_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _index_note(cfg: Config, key: str, src: Path) -> None:
    """Record hash -> source so the cache manager can show real names."""
    data = _index_load(cfg)
    cur = data.get(key)
    if isinstance(cur, dict) and cur.get("source") == str(src):
        return
    data[key] = {"source": str(src),
                 "created": datetime.now().isoformat(timespec="seconds")}
    _index_save(cfg, data)


def _index_gc(cfg: Config) -> None:
    d = audio_cache_dir(cfg)
    idx = _index_load(cfg)
    live = {p.name.split(".")[0] for p in d.glob("*")
            if p.name != "index.json" and ".part" not in p.name}
    stale = [k for k in idx if k not in live]
    if stale:
        for k in stale:
            idx.pop(k, None)
        _index_save(cfg, idx)


def ensure_playable(cfg: Config, src) -> Path:
    """A path /api/audio can serve: playable audio files pass through;
    anything else gets its track extracted into the cache (remux when
    the codec allows, mp3 transcode otherwise). Concurrent-safe via
    part-file + atomic rename."""
    src = Path(src)
    if not src.is_file():
        raise MediaError(f"not found: {src}")
    if not needs_extraction(src):
        return src
    info = probe(src)          # raises MediaError on a trackless video
    d = audio_cache_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    key = cache_key(src)

    # 1) lossless remux when the codec is natively playable
    ext = _copy_targets().get(info.codec)
    if ext:
        dst = d / (key + ext)
        if dst.exists():
            _index_note(cfg, key, src)
            return dst
        tmp = d / (key + ".part" + ext)
        try:
            _ffmpeg(["-i", str(src), "-vn", "-c:a", "copy"], tmp)
            os.replace(tmp, dst)
            log.info("audio cache (remux %s): %s -> %s",
                     info.codec, src.name, dst.name)
            _index_note(cfg, key, src)
            _drop_unplayable(d, key, dst)
            return dst
        except MediaError:
            tmp.unlink(missing_ok=True)
            log.info("remux failed for %s (%s); transcoding to mp3",
                     src.name, info.codec)

    # 2) universal fallback: mp3 192k
    dst = d / (key + ".mp3")
    if not dst.exists():
        tmp = d / (key + ".part.mp3")
        _ffmpeg(["-i", str(src), "-vn", "-c:a", "libmp3lame",
                 "-b:a", "192k"], tmp)
        os.replace(tmp, dst)
        log.info("audio cache (mp3): %s -> %s", src.name, dst.name)
    _index_note(cfg, key, src)
    _drop_unplayable(d, key, dst)
    return dst


def _drop_unplayable(d: Path, key: str, keep: Path) -> None:
    """Remove this key's cache entries the platform cannot decode.

    They are dead weight once a playable one exists (existing_cache skips
    them): an old AAC remux on Linux would otherwise sit in the cache manager
    forever. Best effort -- a file in use stays until the next extraction.
    """
    playable = direct_play_mimes()
    for h in d.glob(key + ".*"):
        if (h != keep and ".part" not in h.name
                and h.suffix.lower() not in playable):
            try:
                h.unlink()
            except OSError:
                pass


# ----------------------------------------------------------------- resolve

def resolve_playable(cfg: Config, override, source) -> dict:
    """Playback resolution shared by the refine preview and the storage
    list: override > source. Directly playable files pass through;
    video/exotic audio resolve to their cache when it exists, else the
    entry is PENDING (first play extracts via /api/audio/prepare)."""
    for cand in (override, source):
        if not (cand and Path(cand).is_file()):
            continue
        p = Path(cand)
        if not needs_extraction(p):
            return {"resolved": str(p), "pending": False,
                    "pending_path": None, "missing": None}
        cache = existing_cache(cfg, p)
        if cache:
            return {"resolved": str(cache), "pending": False,
                    "pending_path": None, "missing": None}
        return {"resolved": None, "pending": True,
                "pending_path": str(p), "missing": None}
    return {"resolved": None, "pending": False,
            "pending_path": None, "missing": override or source}


# ------------------------------------------------------------ cache manager

def list_cache(cfg: Config) -> dict:
    """Cache entries for the settings-card manager. Orphaned files
    without an index entry still list (keyed by file name)."""
    d = audio_cache_dir(cfg)
    idx = _index_load(cfg)
    entries = []
    total = 0
    if d.is_dir():
        for p in sorted(d.glob("*")):
            if p.name == "index.json" or ".part" in p.name:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            total += st.st_size
            src = (idx.get(p.name.split(".")[0]) or {}).get("source")
            entries.append({
                "file": p.name,
                "source": src,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(
                    st.st_mtime).isoformat(timespec="seconds"),
            })
    return {"dir": str(d), "entries": entries, "total_bytes": total}


def delete_cache(cfg: Config, files: list[str]) -> dict:
    """Remove specific cache files. Names must be PLAIN file names inside
    the cache dir -- this must never become an arbitrary-file deleter."""
    d = audio_cache_dir(cfg).resolve()
    removed = 0
    for n in files:
        try:
            p = (audio_cache_dir(cfg) / n).resolve()
        except OSError:
            continue
        if p.parent != d or p.name == "index.json" or not p.is_file():
            continue
        p.unlink(missing_ok=True)
        removed += 1
    _index_gc(cfg)
    out = list_cache(cfg)
    out["removed"] = removed
    return out


def clear_cache(cfg: Config) -> dict:
    """Remove every cached track (and the index -- the whole cache is
    derived data, nothing here is worth keeping)."""
    d = audio_cache_dir(cfg)
    removed = 0
    if d.is_dir():
        for p in d.iterdir():
            if not p.is_file() or p.name == "index.json":
                continue
            p.unlink(missing_ok=True)
            removed += 1
        (d / "index.json").unlink(missing_ok=True)
    out = list_cache(cfg)
    out["removed"] = removed
    return out
