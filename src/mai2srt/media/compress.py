"""CBR MP3 encoding for upload-sized audio files and slices."""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import (
    COMPRESS_TARGET_BYTES,
    MP3_BITRATE_MAX_BPS,
    MP3_BITRATE_MIN_BPS,
    MAX_UPLOAD_BYTES,
    MP3_STANDARD_BITRATES_BPS,
)
from .probe import MediaError, resolve_tool, run_tool

log = logging.getLogger("mai2srt.media")


_UPLOAD_BITRATES = tuple(b for b in MP3_STANDARD_BITRATES_BPS
                         if MP3_BITRATE_MIN_BPS <= b <= MP3_BITRATE_MAX_BPS)


class UploadSizeError(MediaError):
    def __init__(self, label: str, duration_s: float, size: int, bitrate: int):
        self.size = size
        self.bitrate = bitrate
        super().__init__(
            f"{label} ({duration_s:g}s) is {size / 2**20:.1f} MiB "
            f"(> {MAX_UPLOAD_BYTES / 2**20:g} MiB) after encoding at {bitrate // 1000} kbps")


def _floor_bitrate(raw: int) -> int:
    return max((b for b in _UPLOAD_BITRATES if b <= raw),
               default=MP3_BITRATE_MIN_BPS)


def mp3_bitrate_bps(duration_s: float,
                    target_bytes: int = COMPRESS_TARGET_BYTES) -> int:
    """Total bitrate that fits target_bytes over duration_s, clamped."""
    bps = int(target_bytes * 8 / max(duration_s, 0.001))
    return _floor_bitrate(bps)


def encode_mp3(src: Path, dst: Path, bitrate_bps: int,
               start_s: float | None = None, end_s: float | None = None) -> Path:
    """Encode (a slice of) src to mp3 CBR, preserving channels and rate."""
    ffmpeg = resolve_tool("ffmpeg")
    cmd = [ffmpeg, "-y", "-v", "error", "-i", str(src)]
    if start_s is not None:
        cmd += ["-ss", f"{start_s:.3f}"]
    if end_s is not None:
        cmd += ["-to", f"{end_s:.3f}"]
    cmd += ["-vn", "-map_metadata", "-1", "-codec:a", "libmp3lame", "-b:a", str(bitrate_bps), str(dst)]
    r = run_tool(cmd, capture_output=True, text=True,
                 encoding="utf-8", errors="replace", timeout=7200)
    if r.returncode != 0:
        raise MediaError(f"ffmpeg mp3 encode failed: {r.stderr[-300:]}")
    return dst


def compress_for_upload(src: Path, dst: Path, duration_s: float,
                        start_s: float | None = None, end_s: float | None = None,
                        label: str | None = None) -> Path:
    """Encode to MP3 and enforce the upload limit on raw bytes."""
    bitrate = mp3_bitrate_bps(duration_s)
    while True:
        encode_mp3(src, dst, bitrate, start_s=start_s, end_s=end_s)
        size = dst.stat().st_size
        if size <= MAX_UPLOAD_BYTES:
            log.info("compressed to %s (%.1f MiB, %d bps)",
                     dst.name, size / 2**20, bitrate)
            return dst
        lower_rates = [b for b in _UPLOAD_BITRATES if b < bitrate]
        if not lower_rates:
            raise UploadSizeError(label or dst.name, duration_s, size, bitrate)
        scaled = int(bitrate * COMPRESS_TARGET_BYTES / size)
        bitrate = min(lower_rates[-1], _floor_bitrate(scaled))
        log.info("encode overshot (%.1f MiB), re-encoding at %d bps",
                 size / 2**20, bitrate)
