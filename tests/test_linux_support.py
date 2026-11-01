"""Linux support: browser discovery, display check, session export/import,
install hints. Everything platform-dependent is monkeypatched, so these run
identically on every OS."""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mai2srt.preflight as preflight  # noqa: E402
import mai2srt.transcribe.browser as br  # noqa: E402
from mai2srt import cli  # noqa: E402
from mai2srt.config import Config, active_account, cookie_jar_for  # noqa: E402
from mai2srt.session_io import (  # noqa: E402
    SessionIOError, export_session, import_session,
)


def _cfg(tmp_path) -> Config:
    return Config(data_dir=tmp_path / "data")


# ------------------------------------------------------------------ display

def test_display_check_is_a_no_op_off_linux(monkeypatch):
    monkeypatch.setattr(br.sys, "platform", "win32")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert br.display_available() is True


@pytest.mark.parametrize("var", ["DISPLAY", "WAYLAND_DISPLAY"])
def test_display_is_available_with_x11_or_wayland(monkeypatch, var):
    monkeypatch.setattr(br.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv(var, ":0")
    assert br.display_available() is True


def test_headed_launch_without_a_display_fails_with_guidance(monkeypatch, tmp_path):
    import asyncio
    monkeypatch.setattr(br.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with pytest.raises(br.NoDisplayError) as ei:
        asyncio.run(br.launch(object(), _cfg(tmp_path), headless=False))
    assert "session export" in str(ei.value)


# ------------------------------------------------------- browser discovery

def test_linux_hints_cover_chrome_edge_and_system_chromium():
    # the table is chosen at import time; assert the Linux branch is intact
    # by reading the source-of-truth names rather than re-importing
    src = Path(br.__file__).read_text(encoding="utf-8")
    for needle in ("google-chrome", "microsoft-edge", "/opt/google/chrome/chrome",
                   "chromium-system"):
        assert needle in src


def test_find_channel_exe_skips_snap_chromium(monkeypatch, tmp_path):
    monkeypatch.setitem(br.CHANNEL_HINTS, "chromium-system",
                        (["chromium"], ["/nonexistent/chromium"]))
    monkeypatch.setattr(br.shutil, "which", lambda n: "/snap/bin/chromium")
    assert br.find_channel_exe("chromium-system") is None


def test_ubuntu_launcher_shim_for_the_snap_is_skipped(monkeypatch, tmp_path):
    shim = tmp_path / "chromium-browser"
    shim.write_text("#!/bin/sh\nexec /snap/bin/chromium \"$@\"\n", encoding="utf-8")
    real = tmp_path / "chromium"
    real.write_bytes(b"\x7fELF" + b"\0" * 20000)
    monkeypatch.setitem(br.CHANNEL_HINTS, "chromium-system", (["x"], [str(shim)]))
    assert br.find_channel_exe("chromium-system") is None
    monkeypatch.setitem(br.CHANNEL_HINTS, "chromium-system", (["x"], [str(real)]))
    assert br.find_channel_exe("chromium-system") == str(real)


def test_system_chromium_is_the_last_resort_when_bundled_is_absent(monkeypatch, tmp_path):
    monkeypatch.setitem(br.CHANNEL_HINTS, "chromium-system",
                        (["chromium"], []))
    monkeypatch.setattr(br, "find_channel_exe",
                        lambda ch: "/usr/bin/chromium" if ch == "chromium-system" else None)
    monkeypatch.setattr(br, "_bundled_chromium_present", lambda: False)
    assert br.resolve_channel(_cfg(tmp_path)) == "chromium-system"
    monkeypatch.setattr(br, "_bundled_chromium_present", lambda: True)
    assert br.resolve_channel(_cfg(tmp_path)) is None


# --------------------------------------------------------------- preflight

def test_install_hints_are_platform_specific(monkeypatch):
    monkeypatch.setattr(preflight.sys, "platform", "linux")
    h = preflight.install_hints(["ffmpeg", "browser", "session", "llm"])
    assert "apt install ffmpeg" in h["ffmpeg"]
    assert "playwright install chromium" in h["browser"]
    assert "session import" in h["session"]
    assert "llm" not in h
    monkeypatch.setattr(preflight.sys, "platform", "win32")
    assert preflight.install_hints(["ffmpeg", "browser"]) == {}
    assert preflight.platform_name() == "windows"


def test_playwright_cache_dir_is_per_platform(monkeypatch, tmp_path):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(preflight.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(preflight.sys, "platform", "linux")
    (tmp_path / ".cache" / "ms-playwright" / "chromium-1194").mkdir(parents=True)
    assert preflight.bundled_chromium_present() is True
    monkeypatch.setattr(preflight.sys, "platform", "darwin")
    assert preflight.bundled_chromium_present() is False
    (tmp_path / "Library" / "Caches" / "ms-playwright" / "chromium-1194").mkdir(parents=True)
    assert preflight.bundled_chromium_present() is True


# ----------------------------------------------------- session export/import

def _signed_in(cfg: Config, cookies=None) -> None:
    cfg.ensure_dirs()
    cookie_jar_for(cfg, "default").write_text(
        json.dumps(cookies or [{"name": "AppServiceAuthSession", "value": "x",
                                "domain": "playground.microsoft.ai", "path": "/"}]),
        encoding="utf-8")


def test_export_import_roundtrip_moves_the_login(tmp_path):
    src, dst = _cfg(tmp_path / "a"), _cfg(tmp_path / "b")
    _signed_in(src)
    out = tmp_path / "s.json"
    assert export_session(src, out) == "default"
    name = import_session(dst, out)
    assert name == "default" and active_account(dst) == "default"
    assert json.loads(cookie_jar_for(dst, "default").read_text("utf-8")) == \
        json.loads(cookie_jar_for(src, "default").read_text("utf-8"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_exported_session_is_private(tmp_path):
    cfg = _cfg(tmp_path)
    _signed_in(cfg)
    out = tmp_path / "s.json"
    out.write_text("old", encoding="utf-8")
    out.chmod(0o644)                      # a looser pre-existing file
    export_session(cfg, out)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    jar = cookie_jar_for(cfg, "default")
    import_session(cfg, out)
    assert stat.S_IMODE(jar.stat().st_mode) == 0o600


def test_import_can_rename_into_a_named_account(tmp_path):
    src, dst = _cfg(tmp_path / "a"), _cfg(tmp_path / "b")
    _signed_in(src)
    out = tmp_path / "s.json"
    export_session(src, out)
    assert import_session(dst, out, account="server") == "server"
    assert active_account(dst) == "server"
    assert cookie_jar_for(dst, "server").exists()


def test_export_without_a_session_explains_itself(tmp_path):
    with pytest.raises(SessionIOError, match="login"):
        export_session(_cfg(tmp_path), tmp_path / "s.json")


@pytest.mark.parametrize("body", ["not json", "[]", '{"format": "other"}',
                                  '{"format": "mai2srt-session", "version": 1}',
                                  '{"format": "mai2srt-session", "version": 99, "cookies": []}'])
def test_import_rejects_foreign_files(tmp_path, body):
    f = tmp_path / "bad.json"
    f.write_text(body, encoding="utf-8")
    with pytest.raises(SessionIOError):
        import_session(_cfg(tmp_path), f)


def test_session_cli_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("MAI2SRT_HOME", str(tmp_path / "home"))
    cfg = Config()
    _signed_in(cfg)
    out = tmp_path / "s.json"
    assert cli.main(["session", "export", str(out)]) == 0
    monkeypatch.setenv("MAI2SRT_HOME", str(tmp_path / "other"))
    assert cli.main(["session", "import", str(out)]) == 0
    assert cookie_jar_for(Config(), "default").exists()
    assert cli.main(["session", "import", str(tmp_path / "missing.json")]) == 1
