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

### Media playback on Linux

WebKitGTK plays the refine page's audio through GStreamer. A stock install has gst-plugins-base and -good only, which is also what the AppImage bundles, and that set has no AAC decoder: an AAC stream fails with `Missing decoder: MPEG-4 AAC`. mp3, Opus, Vorbis, FLAC and WAV decode with it.

The backend therefore decides per platform which files `/api/audio` serves as they are (`media/playback.py`, `direct_play_mimes()`). On Linux, AAC — video soundtracks and `.m4a` / `.aac` files — goes through the audio extraction cache as mp3. An old `.m4a` cache entry is not a hit and is deleted once its mp3 replacement exists. Windows (WebView2) and macOS keep the lossless remux. Do not drop this rule by adding gst-libav to one package: the `.deb` uses the system's GStreamer, which may not have it.

## Build and release flow

`.github/workflows/release.yml` builds Linux `.deb` and AppImage alongside the existing Windows NSIS installer. Branch pushes and manual runs are for validation and upload workflow artifacts; formal GitHub Release publishing remains gated on version tags. Do not treat a branch artifact as a published release.

## Verification

`scripts/release-linux.sh` builds the backend, stages its complete onedir resources, builds both packages, checks package contents and dependencies, then extracts the packaged backend and calls `/api/system`. The smoke test must pass for the staged backend and for each generated package before the workflow artifact is uploaded.
