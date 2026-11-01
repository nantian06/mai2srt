<!-- Rename to v<version>.md when tagging: release.yml reads docs/release-notes/<tag>.md as the release body. -->
# mai2srt (unreleased)

新增批量转录命令与 Linux 支持（命令行 / 源码运行）。Windows 桌面应用的行为不变。

## 批量转录：`mai2srt batch`

- `mai2srt batch <文件或文件夹…>`：一次处理多个音视频，`-r` 递归子文件夹，`--out-dir` 把字幕集中到一个文件夹。
- 整批共用一个浏览器会话，默认串行；`--jobs 2|3` 可并行上传（上限 3）。
- 可重跑：转录结果与 `.srt` 都在的文件自动跳过；只有转录结果的文件只重新断句，不再上传。`--overwrite` 全部重做。
- 单个文件失败不影响其余文件；未登录、会话过期、生物特征通知（HTTP 451）、没有可用浏览器这类全局问题会立即中止整批并给出处理办法。
- `--dry-run` 只列出计划，`--report` 输出 JSON 汇总，结束时打印汇总表；有失败则退出码非 0，Ctrl-C 退出码 130。

## Linux

- 后端与 CLI 可在 Linux 上从源码运行：自动探测 Google Chrome / Microsoft Edge / 发行版 chromium，找不到时回退到 playwright 自带 chromium；FFmpeg 走 `PATH`，缺失时给出 apt / dnf / pacman 安装提示。
- 无显示器的机器：`mai2srt session export/import` 把登录态从桌面机器搬到服务器（导出文件权限 600）。
- 桌面应用仅有开发模式（`./dev.sh`），暂无 Linux 安装包。
- 新增 PR 级 CI，在 Ubuntu 与 Windows 上运行测试。

---

## English

Adds a batch transcription command and Linux support (CLI / from source). The Windows desktop app behaves as before.

## Batch transcription: `mai2srt batch`

- `mai2srt batch <files or folders…>` handles many media files in one run; `-r` descends into subfolders and `--out-dir` collects the subtitles in one folder.
- One browser session for the whole batch, serial by default; `--jobs 2|3` uploads in parallel (capped at 3).
- Re-runnable: files with both a transcript and an `.srt` are skipped; files with only a transcript are just re-segmented, never uploaded again. `--overwrite` redoes everything.
- A failing file does not stop the others; problems that would sink every file — not signed in, an expired session, the biometric notice (HTTP 451), no usable browser — abort the batch at once with the fix.
- `--dry-run` lists the plan, `--report` writes a JSON summary, and a summary table is printed at the end; the exit code is non-zero if anything failed (130 on Ctrl-C).

## Linux

- The backend and CLI run from source on Linux: Google Chrome / Microsoft Edge / the distro's chromium are detected automatically, with playwright's own chromium as the fallback; FFmpeg comes from `PATH`, and a missing one gets apt / dnf / pacman install hints.
- Machines without a display: `mai2srt session export/import` carries the sign-in over from a desktop machine (the exported file is chmod 600).
- The desktop app is dev-mode only (`./dev.sh`) for now; there is no Linux installer.
- New PR-level CI runs the tests on Ubuntu and Windows.
