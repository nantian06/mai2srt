#!/usr/bin/env python3
"""Start the staged frozen backend on a temporary home and verify /api/system."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <staged-backend>", file=sys.stderr)
        return 2

    executable = Path(sys.argv[1]).resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        print(f"staged backend is missing or not executable: {executable}", file=sys.stderr)
        return 1

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    with tempfile.TemporaryDirectory(prefix="mai2srt-linux-smoke-") as temp:
        home = Path(temp)
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["MAI2SRT_HOME"] = str(home / ".mai2srt")
        process = subprocess.Popen(
            [str(executable), "serve", "--port", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 30
            url = f"http://127.0.0.1:{port}/api/system"
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    with urlopen(url, timeout=1) as response:
                        body = json.loads(response.read().decode("utf-8"))
                        if response.status == 200 and isinstance(body, dict):
                            print("    staged backend answered /api/system with HTTP 200")
                            return 0
                except (OSError, URLError, TimeoutError, json.JSONDecodeError):
                    time.sleep(0.25)

            log = home / ".mai2srt" / "logs" / "backend.log"
            if log.is_file():
                tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-20:])
                if tail:
                    print("backend log tail:\n" + tail, file=sys.stderr)
            print(f"staged backend failed to answer /api/system (exit={process.poll()})", file=sys.stderr)
            return 1
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
