"""External media tools must not inherit the app's own library directory.

A packaged Linux build gets ``LD_LIBRARY_PATH`` from PyInstaller's bootloader,
pointing at the bundle's ``_internal``. That directory carries a ``libstdc++``
older than the one on a current distro, so ``ffprobe`` -- resolved from PATH, and
linked against system libraries that need a newer ``GLIBCXX`` -- loads the bundled
copy instead and dies before reading a frame:

    ffprobe: .../_internal/libstdc++.so.6: version `GLIBCXX_3.4.32' not found
             (required by /lib/x86_64-linux-gnu/libsrt-gnutls.so.1.5)

Every symptom is a failed probe of a video file, so it reads like an FFmpeg
install problem while the app is the thing shadowing the system library. It cannot
appear in a dev run (no bundle, no injected path) nor on the build machine (its
``libstdc++`` matches what it bundled), which is why it reaches a user first.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.media import probe  # noqa: E402

BUNDLE = "/opt/mai2srt/backend/mai2srt-backend/_internal"


def _frozen(monkeypatch, meipass: str = BUNDLE) -> None:
    """Pretend to be the packaged sidecar: PyInstaller has set _MEIPASS."""
    monkeypatch.setattr(sys, "_MEIPASS", meipass, raising=False)


def _posix(monkeypatch) -> None:
    monkeypatch.setattr(probe.sys, "platform", "linux")


def test_bundle_entry_is_dropped_and_others_survive(monkeypatch):
    _posix(monkeypatch)
    _frozen(monkeypatch)
    other = "/usr/local/lib"
    monkeypatch.setenv("LD_LIBRARY_PATH", f"{BUNDLE}{os.pathsep}{other}")
    env = probe.child_env()
    assert env is not None
    assert env["LD_LIBRARY_PATH"] == other
    assert env["PATH"] == os.environ["PATH"], "a full environment must be kept"


def test_variable_is_removed_when_the_bundle_was_the_only_entry(monkeypatch):
    _posix(monkeypatch)
    _frozen(monkeypatch)
    monkeypatch.setenv("LD_LIBRARY_PATH", BUNDLE)
    env = probe.child_env()
    assert env is not None
    assert "LD_LIBRARY_PATH" not in env


def test_dev_run_is_left_alone(monkeypatch):
    """No _MEIPASS: a checkout has no bundle to shadow anything."""
    _posix(monkeypatch)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", BUNDLE)
    assert probe.child_env() is None


def test_a_foreign_library_path_is_not_mistaken_for_the_bundle(monkeypatch):
    """A user-set LD_LIBRARY_PATH we did not inject must survive untouched."""
    _posix(monkeypatch)
    _frozen(monkeypatch)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/some/other/dir")
    assert probe.child_env() is None


def test_windows_is_a_no_op(monkeypatch):
    monkeypatch.setattr(probe.sys, "platform", "win32")
    _frozen(monkeypatch)
    monkeypatch.setenv("LD_LIBRARY_PATH", BUNDLE)
    assert probe.child_env() is None


def test_trailing_separators_still_match(monkeypatch):
    """The bootloader and _MEIPASS need not spell the same directory alike."""
    _posix(monkeypatch)
    _frozen(monkeypatch)
    monkeypatch.setenv("LD_LIBRARY_PATH", BUNDLE + "/")
    env = probe.child_env()
    assert env is not None and "LD_LIBRARY_PATH" not in env


def test_run_tool_passes_the_cleaned_env_to_a_real_child(monkeypatch, tmp_path):
    """End to end: the child itself reports what it inherited."""
    _posix(monkeypatch)
    bundle = str(tmp_path / "_internal")
    _frozen(monkeypatch, bundle)
    monkeypatch.setenv("LD_LIBRARY_PATH", bundle)
    r = probe.run_tool([sys.executable, "-c",
                        "import os; print(os.environ.get('LD_LIBRARY_PATH', ''))"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert bundle not in r.stdout


def test_run_tool_does_not_mutate_the_apps_own_environment(monkeypatch, tmp_path):
    """The sidecar still needs the bundle path for its own library loading."""
    _posix(monkeypatch)
    seen: dict = {}

    def fake(cmd, **kwargs):
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(probe.subprocess, "run", fake)
    bundle = str(tmp_path / "_internal")
    _frozen(monkeypatch, bundle)
    monkeypatch.setenv("LD_LIBRARY_PATH", bundle)
    probe.run_tool(["ffprobe", "-v", "error"], capture_output=True)
    assert os.environ["LD_LIBRARY_PATH"] == bundle
    assert seen["kwargs"]["env"] is not None
    assert "LD_LIBRARY_PATH" not in seen["kwargs"]["env"]


def test_a_callers_own_env_is_respected(monkeypatch, tmp_path):
    _posix(monkeypatch)
    seen: dict = {}

    def fake(cmd, **kwargs):
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(probe.subprocess, "run", fake)
    bundle = str(tmp_path / "_internal")
    _frozen(monkeypatch, bundle)
    monkeypatch.setenv("LD_LIBRARY_PATH", bundle)
    mine = {"PATH": "/bin", "LD_LIBRARY_PATH": "/custom"}
    probe.run_tool(["ffprobe"], env=mine)
    assert seen["kwargs"]["env"] == mine
