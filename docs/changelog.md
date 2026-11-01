# Maintenance Changelog

| Date | Type | Change | Impact |
| --- | --- | --- | --- |
| 2026-11-01 | CI / packaging | Added Linux x86_64 `.deb` and AppImage builds, Linux resource-based PyInstaller onedir startup, package-content checks, and extracted-backend smoke tests. Preserved the Windows NSIS path and tag-only Release publishing. | GitHub Actions run [41294456851](https://github.com/nantian06/mai2srt/actions/runs/41294456851) built both Linux packages and the Windows installer; the backend in each Linux package returned HTTP 200 from `/api/system`. The run uploaded workflow artifacts only; it did not publish a Release. Draft review: [PR #4](https://github.com/nantian06/mai2srt/pull/4). |
