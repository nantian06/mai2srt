# Packaging and Runtime Design

## Scope

This document describes the desktop packaging boundary. Application behavior and the Python backend API remain unchanged.

## Runtime layout

- The Tauri frontend owns the desktop window and invokes the Python backend over the local service interface.
- Windows keeps the existing Tauri `externalBin` sidecar layout and NSIS installer.
- Linux packages the PyInstaller **onedir** output as Tauri resources. The complete `backend/mai2srt-backend/` tree, including `_internal`, must remain together; Linux resolves the executable from `BaseDirectory::Resource` rather than treating it as a single external binary.
- The Linux platform config is an overlay (`tauri.linux.conf.json`), so the Windows base sidecar configuration remains unchanged.

## Linux package matrix

| Package | Build baseline | FFmpeg | Desktop runtime |
| --- | --- | --- | --- |
| `.deb` | Ubuntu 22.04, x86_64 | Declared as a package dependency | GTK 3 and WebKitGTK 4.1 runtime libraries |
| AppImage | Ubuntu 22.04, x86_64 | Not bundled; `ffmpeg` and `ffprobe` must be on `PATH` | GStreamer/media framework bundling is enabled; host FUSE support may be needed |

## Build and release flow

`.github/workflows/release.yml` builds Linux `.deb` and AppImage alongside the existing Windows NSIS installer. Branch pushes and manual runs are for validation and upload workflow artifacts; formal GitHub Release publishing remains gated on version tags. Do not treat a branch artifact as a published release.

## Verification

`scripts/release-linux.sh` builds the backend, stages its complete onedir resources, builds both packages, checks package contents and dependencies, then extracts the packaged backend and calls `/api/system`. The smoke test must pass for the staged backend and for each generated package before the workflow artifact is uploaded.
