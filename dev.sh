#!/usr/bin/env bash
# Linux/macOS counterpart of dev.ps1: backend + vite + tauri dev from the repo
# root. Set MAI2SRT_PYTHON to pick the interpreter (default: python3).
set -euo pipefail
cd "$(dirname "$0")/app"
exec ./node_modules/.bin/tauri dev
