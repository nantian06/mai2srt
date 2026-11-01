#!/usr/bin/env bash
# Build the Linux desktop backend sidecar and Tauri .deb + AppImage bundles.
# Run from any directory after installing the workflow's Linux dependencies.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$ROOT/app"
TAURI_DIR="$APP_DIR/src-tauri"
STAGE_DIR="$TAURI_DIR/backend"
DIST_DIR="$ROOT/dist"

cd "$ROOT"

echo "==> [1/6] checking versions and required tools ..."
VERSION="$(python3 - "$ROOT" <<'PY'
import json
import re
import sys
import tomllib
from pathlib import Path

root = Path(sys.argv[1])
pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
cargo = tomllib.loads((root / "app/src-tauri/Cargo.toml").read_text(encoding="utf-8"))
package = json.loads((root / "app/package.json").read_text(encoding="utf-8"))
tauri = json.loads((root / "app/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
init = (root / "src/mai2srt/__init__.py").read_text(encoding="utf-8")
match = re.search(r'^__version__ = "([^"]+)"$', init, re.MULTILINE)
if not match:
    raise SystemExit("version field not found in src/mai2srt/__init__.py")
versions = {
    "pyproject.toml": pyproject["project"]["version"],
    "src/mai2srt/__init__.py": match.group(1),
    "app/package.json": package["version"],
    "app/src-tauri/tauri.conf.json": tauri["version"],
    "app/src-tauri/Cargo.toml": cargo["package"]["version"],
}
for path, version in versions.items():
    print(f"    {path}: {version}", file=sys.stderr)
if len(set(versions.values())) != 1:
    raise SystemExit("version drift detected")
print(next(iter(versions.values())))
PY
)"
echo "    release version: $VERSION"
command -v ffmpeg >/dev/null || { echo "ffmpeg is required on PATH" >&2; exit 1; }
command -v ffprobe >/dev/null || { echo "ffprobe is required on PATH" >&2; exit 1; }

if [[ ! -f "$ROOT/packaging/linux/ffmpeg-README.txt" ]]; then
    echo "missing packaging/linux/ffmpeg-README.txt" >&2
    exit 1
fi

echo "==> [2/6] building the frozen Python sidecar ..."
python3 -m PyInstaller packaging/backend.spec --noconfirm --clean --distpath "$ROOT/dist-backend"
BUILT_DIR="$ROOT/dist-backend/mai2srt-backend"
BUILT_EXE="$BUILT_DIR/mai2srt-backend"
if [[ ! -x "$BUILT_EXE" || ! -d "$BUILT_DIR/_internal" ]]; then
    echo "PyInstaller output is incomplete: $BUILT_DIR" >&2
    exit 1
fi

echo "==> [3/6] staging sidecar and notices ..."
mkdir -p "$STAGE_DIR/mai2srt-backend" "$STAGE_DIR/ffmpeg" "$STAGE_DIR/licenses"
cp -a "$BUILT_DIR/." "$STAGE_DIR/mai2srt-backend/"
cp "$ROOT/packaging/linux/ffmpeg-README.txt" "$STAGE_DIR/ffmpeg/README.txt"
cp "$ROOT/LICENSE" "$ROOT/THIRD-PARTY-NOTICES.md" "$STAGE_DIR/licenses/"
cp "$ROOT"/licenses/*.txt "$STAGE_DIR/licenses/"

echo "==> [4/6] smoke-testing the staged sidecar ..."
python3 "$ROOT/scripts/smoke-linux-backend.py" "$STAGE_DIR/mai2srt-backend/mai2srt-backend"

echo "==> [5/6] building .deb and AppImage ..."
(
    cd "$APP_DIR"
    pnpm exec tauri build --bundles deb,appimage
)

echo "==> [6/6] validating and collecting artifacts ..."
shopt -s nullglob
DEB_FILES=("$TAURI_DIR"/target/release/bundle/deb/*.deb)
APPIMAGE_FILES=("$TAURI_DIR"/target/release/bundle/appimage/*.AppImage)
if [[ ${#DEB_FILES[@]} -ne 1 ]]; then
    echo "expected one .deb, found ${#DEB_FILES[@]}" >&2
    exit 1
fi
if [[ ${#APPIMAGE_FILES[@]} -ne 1 ]]; then
    echo "expected one AppImage, found ${#APPIMAGE_FILES[@]}" >&2
    exit 1
fi
mkdir -p "$DIST_DIR"
cp "${DEB_FILES[0]}" "$DIST_DIR/"
cp "${APPIMAGE_FILES[0]}" "$DIST_DIR/"
chmod 0755 "$DIST_DIR/$(basename "${APPIMAGE_FILES[0]}")"

dpkg-deb --info "${DEB_FILES[0]}"
DEB_DEPENDS="$(dpkg-deb -f "${DEB_FILES[0]}" Depends)"
if [[ "$DEB_DEPENDS" != *ffmpeg* ]]; then
    echo ".deb metadata does not declare the ffmpeg runtime dependency: $DEB_DEPENDS" >&2
    exit 1
fi
DEB_CONTENTS="$(dpkg-deb --contents "${DEB_FILES[0]}")"
if [[ "$DEB_CONTENTS" != *"backend/mai2srt-backend/mai2srt-backend"* ]]; then
    echo ".deb does not contain the Linux backend sidecar" >&2
    exit 1
fi
file "${APPIMAGE_FILES[0]}"
if [[ ! -x "$DIST_DIR/$(basename "${APPIMAGE_FILES[0]}")" ]]; then
    echo "AppImage is not executable" >&2
    exit 1
fi
EXTRACT_DIR="$(mktemp -d)"
trap 'rm -rf "$EXTRACT_DIR"' EXIT
DEB_ROOT="$EXTRACT_DIR/deb"
mkdir -p "$DEB_ROOT"
dpkg-deb --extract "${DEB_FILES[0]}" "$DEB_ROOT"
DEB_SIDECAR="$(find "$DEB_ROOT" -type f -path '*/backend/mai2srt-backend/mai2srt-backend' -print -quit)"
if [[ -z "$DEB_SIDECAR" || ! -x "$DEB_SIDECAR" || ! -d "$(dirname "$DEB_SIDECAR")/_internal" ]]; then
    echo ".deb backend onedir resources are missing or incomplete" >&2
    exit 1
fi
python3 "$ROOT/scripts/smoke-linux-backend.py" "$DEB_SIDECAR"
APPIMAGE_ROOT="$EXTRACT_DIR/appimage"
mkdir -p "$APPIMAGE_ROOT"
(
    cd "$APPIMAGE_ROOT"
    "${APPIMAGE_FILES[0]}" --appimage-extract >/dev/null
)
SIDECAR_PATH="$(find "$APPIMAGE_ROOT/squashfs-root" -type f -path '*/backend/mai2srt-backend/mai2srt-backend' -print -quit)"
if [[ -z "$SIDECAR_PATH" || ! -x "$SIDECAR_PATH" || ! -d "$(dirname "$SIDECAR_PATH")/_internal" ]]; then
    echo "AppImage backend onedir resources are missing or incomplete" >&2
    exit 1
fi
python3 "$ROOT/scripts/smoke-linux-backend.py" "$SIDECAR_PATH"
rm -rf "$EXTRACT_DIR"
trap - EXIT

printf '\n==> Linux artifacts for %s:\n' "$VERSION"
find "$DIST_DIR" -maxdepth 1 -type f \( -name '*.deb' -o -name '*.AppImage' \) -printf '    %f (%s bytes)\n' | sort
