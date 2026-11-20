# Maintenance Changelog

| Date | Type | Change | Impact |
| --- | --- | --- | --- |
| 2026-11-01 | Release | v0.3.0: version bumped in the five tracked places plus `Cargo.lock`; release notes in `docs/release-notes/v0.3.0.md`. | First release whose tag also publishes the Linux `.deb` and AppImage. |
| 2026-11-01 | Pipeline / GUI / server | Batches run as a three-stage pipeline (prepare with ffmpeg, upload to the playground, segment with the LLM), each stage with its own workers; preparation runs one file ahead. The queue accepts files while it runs (`POST /api/jobs/{id}/files`, `DELETE /api/jobs/{id}/files/{index}`), and the GUI sends every media selection through it: files dropped during a transcription join the queue, a finished file is skipped with a row-level "re-transcribe", and the LLM concurrency is a setting (`GET/PUT /api/batch/settings`, CLI `--llm-jobs`, default 1). | Several files no longer wait for each other's LLM segmentation; a file dropped mid-run is queued instead of refused. Covered by `tests/test_batch.py` and `tests/test_batch_server.py`, plus a browser walk-through against a faked playground (three files observed in three phases at once). |
| 2026-11-01 | Playback (Linux) | The set of directly playable audio formats is per platform. On Linux, AAC (video soundtracks, `.m4a`, `.aac`) goes through the extraction cache as mp3, and an old `.m4a` cache entry is ignored and replaced. | Fixes a silent play button on the refine page in the Linux app: WebKitGTK plays through GStreamer, and with only the base and good plugins (a stock install, and what the AppImage bundles) there is no AAC decoder (`Missing decoder: MPEG-4 AAC`, reproduced with `gst-launch-1.0`). Covered by `tests/test_m6k_video.py`. |
| 2026-11-01 | GUI / server | Added a Tasks page (task monitor) and moved job state out of the Transcribe page into an app-wide store, so a running transcription or batch stays visible after switching pages or reloading the UI. The job API now reports `created_at` / `finished_at` / `meta` / `events`, resumes a stream from `from_idx`, and `DELETE /api/jobs/{id}` removes a finished job. | Fixes the running task disappearing from the Transcribe page after a page switch (the job itself kept running). Completion notices now fire whichever page is open. Covered by `tests/test_jobs_monitor.py` and a browser walk-through against a faked playground. |
| 2026-11-01 | CI / packaging | Added Linux x86_64 `.deb` and AppImage builds, Linux resource-based PyInstaller onedir startup, package-content checks, and extracted-backend smoke tests. Preserved the Windows NSIS path and tag-only Release publishing. | GitHub Actions run [41294456851](https://github.com/nantian06/mai2srt/actions/runs/41294456851) built both Linux packages and the Windows installer; the backend in each Linux package returned HTTP 200 from `/api/system`. The run uploaded workflow artifacts only; it did not publish a Release. Draft review: [PR #4](https://github.com/nantian06/mai2srt/pull/4). |


## 2026-11-13 固定文档基线接入（未发布）

这是文档维护记录，不是新增软件发布或部署；已知交付日期沿下列原始来源。更早前序未证明的部分标为缺口，不能从当前代码倒推当时决定。

### 当前可证实基线

- 身份 / 轨道：源码main HEAD f3087a19e85da1ef7c0a21bd6e9bf8b2af7591df / 包0.3.0。
- 源码：当前源码为 `f3087a19e85da1ef7c0a21bd6e9bf8b2af7591df`（`main`）；维护前无未提交差异，本轮仅增加文档维护改动。 维护前状态：`clean（本次维护前）`。
- 前序关系：下列已知里程碑仅在证据明确时相连；不假定本轮记录前没有其他版本。
- 本次变化：现行入口补基线和历史导航，阶段流水保全，业务代码、配置与部署未修改。HEAD比v0.3.0 tag多Linux AAC设计说明提交；v0.3.0流水线队列、运行中追加和Linux包，旧release notes继续保留。
- 需求及覆盖：长音视频转录与精修导出；队列跨页面可见、流水线并行与Linux播放可用。
- 架构与设计依据：Linux PyInstaller onedir资源树保全；WebKitGTK缺AAC解码器时转MP3，Windows保留原路径。 未记录的备选方案与动机保持待核。
- 验证 / 部署：已有changelog记录隔离测试、fake playground浏览器 walkthrough及构建smoke；不追加模型或真实转录。
- 兼容 / 恢复：Linux需系统FFmpeg/WebKitGTK；分支CI产物不等于正式Release；未以此次文档维护验证桌面/账号。

### 固定文档依据

下表是本次维护前的完整**已检索文档集合**，不是完整项目需求或源码快照。SHA256固定这次原件；回顾性叙述不冒充原发布当时的文档。Git字段均已用本地 `git cat-file` 核实，运行登记保留原日期，未向远端取证。

| 文档 | 固定原件 / SHA256 | Git文档定位 |
| --- | --- | --- |
| `README.md` | `cd4103e1af7c10649aefc06411b1f89f7d9828128ec65a495d2b236dfbd1f2fa` | `f3087a19e85da1ef7c0a21bd6e9bf8b2af7591df:README.md` |
| `docs/design.md` | `df6cf43231f8fa5c59c618e38a347addf6b223b3e89398ac75e01de393fd6dfb` | `f3087a19e85da1ef7c0a21bd6e9bf8b2af7591df:docs/design.md` |
| `docs/changelog.md` | `c0c2b41636c6372195e736a70fa30eb545f857b62178a6dc7dde3c39a1136402` | `f3087a19e85da1ef7c0a21bd6e9bf8b2af7591df:docs/changelog.md` |
| `pyproject.toml` | `40a296775013c6f8f1337729f7f8963dd00b21619b32586cc124d6d2dc63f3c0` | `f3087a19e85da1ef7c0a21bd6e9bf8b2af7591df:pyproject.toml` |
| `docs/release-notes/v0.1.2.md` | `deb6e7298d807b8e6e63ed578b393884a2712048628051af60997b84a90db90b` | `f3087a19e85da1ef7c0a21bd6e9bf8b2af7591df:docs/release-notes/v0.1.2.md` |
| `docs/release-notes/v0.3.0.md` | `11a227d159634f460b6e31985f9e323a8cf3aea37eacb6294cf5e8b89ff24b17` | `f3087a19e85da1ef7c0a21bd6e9bf8b2af7591df:docs/release-notes/v0.3.0.md` |
| `docs/release-notes/v0.1.3.md` | `449916ab0c5416b2733a0e8a8002795194de47da52d6f414204517f3ff36c617` | `f3087a19e85da1ef7c0a21bd6e9bf8b2af7591df:docs/release-notes/v0.1.3.md` |
| `docs/release-notes/v0.1.1.md` | `39c0d82718a3d781f6ad2bef06508f24f103c39caba9ec9a43bdd1dc1164f446` | `f3087a19e85da1ef7c0a21bd6e9bf8b2af7591df:docs/release-notes/v0.1.1.md` |
| `docs/release-notes/v0.1.4.md` | `3956aa266415dcb0df35f201553f9ecbfda33718b84b0728008b761a7eec51bf` | `f3087a19e85da1ef7c0a21bd6e9bf8b2af7591df:docs/release-notes/v0.1.4.md` |
| `docs/release-notes/v0.1.0.md` | `83a5e5d6508ddebb7fadef199312667f5a087dcf36026ce9de5dfb62ec728002` | `f3087a19e85da1ef7c0a21bd6e9bf8b2af7591df:docs/release-notes/v0.1.0.md` |
| `docs/release-notes/v0.2.0.md` | `d48dd272c634269a0cb523ef6ba51b77c6bb7423ebd39cf875bb8894a6ab2048` | `f3087a19e85da1ef7c0a21bd6e9bf8b2af7591df:docs/release-notes/v0.2.0.md` |

完整提交均指本仓库；用 `git show <commit>:<path>` 读取对应文档。本轮未提交的文档增量先作为待发布维护记录，原版文档从上述HEAD还原。

### 历史缺口和本次检索边界

- v0.1.0–0.1.4本地仅release notes，缺对应本地tag和原发布完整需求/设计；不能用当前HEAD设计冒充旧版设计。
- 更早版本的当时完整需求/架构/设计与决策链未全部补齐；缺口登记不等于历史完整性通过。
- 检索：本项目 README / SOURCE_ENTRY、存在的当前设计与部署入口、版本文件、既有 docs/changelog 和 release notes、对应 archive/mai2srt/docs 的Markdown导航；Git项目检查当前HEAD/branch/status、本地最近6条日志与全部本地tags；仅定向读取所列关联任务。未fetch，未扫描私有配置、媒体或业务库，也未遍历所有历史任务。
- 2026-11-13之后如文档变化，需要在后续记录绑定新的准确提交或冻结原件；此记录不自动覆盖后续状态。

### 已知自有交付里程碑

以下日期明确区分任务/观察/tag提交与发布日期。每项需求变化限于“变化”栏；未说明的需求决定、备选方案、原发布完整设计和兼容/验收结论均为未记录，不能自动沿用当前设计。回顾来源为上文固定原件，原任务保留原职责。

#### v0.1.0

- 身份与前序：mai2srt v0.1.0；前序 无前序记录。本地未发现对应tag，原发布日期和准确源码提交待核。
- 变化及需求覆盖：首个桌面转录/精修/SRT公开版。完整说明见 [release notes](release-notes/v0.1.0.md)。
- 固定文档：本仓库 `f3087a19e85da1ef7c0a21bd6e9bf8b2af7591df:docs/release-notes/v0.1.0.md`。这是当前HEAD收录的历史说明，不冒充该版原始完整源码。
- 设计/取舍与完整基线：release notes已记载的原因沿原文；缺少该版当时完整需求/架构/设计与未采用方案，不以当前打包设计替代。
- 交付/验证/兼容：安装与兼容范围沿该说明，测试原日志/设备验收/安装包摘要若未在说明中固定则未记录；本次不补造发布或运行验收。
#### v0.1.1

- 身份与前序：mai2srt v0.1.1；前序 0.1.0。本地未发现对应tag，原发布日期和准确源码提交待核。
- 变化及需求覆盖：精修拆分交互与中间状态修复、长上下文预算。完整说明见 [release notes](release-notes/v0.1.1.md)。
- 固定文档：本仓库 `f3087a19e85da1ef7c0a21bd6e9bf8b2af7591df:docs/release-notes/v0.1.1.md`。这是当前HEAD收录的历史说明，不冒充该版原始完整源码。
- 设计/取舍与完整基线：release notes已记载的原因沿原文；缺少该版当时完整需求/架构/设计与未采用方案，不以当前打包设计替代。
- 交付/验证/兼容：安装与兼容范围沿该说明，测试原日志/设备验收/安装包摘要若未在说明中固定则未记录；本次不补造发布或运行验收。
#### v0.1.2

- 身份与前序：mai2srt v0.1.2；前序 0.1.1。本地未发现对应tag，原发布日期和准确源码提交待核。
- 变化及需求覆盖：按供应商发送思考参数，模型能力和API格式选择。完整说明见 [release notes](release-notes/v0.1.2.md)。
- 固定文档：本仓库 `f3087a19e85da1ef7c0a21bd6e9bf8b2af7591df:docs/release-notes/v0.1.2.md`。这是当前HEAD收录的历史说明，不冒充该版原始完整源码。
- 设计/取舍与完整基线：release notes已记载的原因沿原文；缺少该版当时完整需求/架构/设计与未采用方案，不以当前打包设计替代。
- 交付/验证/兼容：安装与兼容范围沿该说明，测试原日志/设备验收/安装包摘要若未在说明中固定则未记录；本次不补造发布或运行验收。
#### v0.1.3

- 身份与前序：mai2srt v0.1.3；前序 0.1.2。本地未发现对应tag，原发布日期和准确源码提交待核。
- 变化及需求覆盖：解释HTTP451并添加由用户本人确认的生物特征通知入口。完整说明见 [release notes](release-notes/v0.1.3.md)。
- 固定文档：本仓库 `f3087a19e85da1ef7c0a21bd6e9bf8b2af7591df:docs/release-notes/v0.1.3.md`。这是当前HEAD收录的历史说明，不冒充该版原始完整源码。
- 设计/取舍与完整基线：release notes已记载的原因沿原文；缺少该版当时完整需求/架构/设计与未采用方案，不以当前打包设计替代。
- 交付/验证/兼容：安装与兼容范围沿该说明，测试原日志/设备验收/安装包摘要若未在说明中固定则未记录；本次不补造发布或运行验收。
#### v0.1.4

- 身份与前序：mai2srt v0.1.4；前序 0.1.3。本地未发现对应tag，原发布日期和准确源码提交待核。
- 变化及需求覆盖：长音频上限降29分钟与仅音频静音检测进度。完整说明见 [release notes](release-notes/v0.1.4.md)。
- 固定文档：本仓库 `f3087a19e85da1ef7c0a21bd6e9bf8b2af7591df:docs/release-notes/v0.1.4.md`。这是当前HEAD收录的历史说明，不冒充该版原始完整源码。
- 设计/取舍与完整基线：release notes已记载的原因沿原文；缺少该版当时完整需求/架构/设计与未采用方案，不以当前打包设计替代。
- 交付/验证/兼容：安装与兼容范围沿该说明，测试原日志/设备验收/安装包摘要若未在说明中固定则未记录；本次不补造发布或运行验收。
#### v0.2.0

- 身份与前序：mai2srt v0.2.0；前序 0.1.4。本地tag提交日期2026-11-01，正式发布时刻未在本次远端核验。
- 变化及需求覆盖：批量队列、Linux CLI和会话搬运，无Linux安装包。完整说明见 [release notes](release-notes/v0.2.0.md)。
- 固定文档：本仓库 `fa5c13faa89dd912115e994fb81db9e20600b0ce:docs/release-notes/v0.2.0.md`。这是本地tag的固定发布说明。
- 设计/取舍与完整基线：release notes已记载的原因沿原文；缺少该版当时完整需求/架构/设计与未采用方案，不以当前打包设计替代。
- 交付/验证/兼容：安装与兼容范围沿该说明，测试原日志/设备验收/安装包摘要若未在说明中固定则未记录；本次不补造发布或运行验收。
#### v0.3.0

- 身份与前序：mai2srt v0.3.0；前序 0.2.0。本地tag提交日期2026-11-01，正式发布时刻未在本次远端核验。
- 变化及需求覆盖：三阶段流水线、运行中追加、Linux包与AAC转MP3。完整说明见 [release notes](release-notes/v0.3.0.md)。
- 固定文档：本仓库 `63336e73c80e897b3b525f6e67e914934f08780c:docs/release-notes/v0.3.0.md`。这是本地tag的固定发布说明。
- 设计/取舍与完整基线：release notes已记载的原因沿原文；缺少该版当时完整需求/架构/设计与未采用方案，不以当前打包设计替代。
- 交付/验证/兼容：安装与兼容范围沿该说明，测试原日志/设备验收/安装包摘要若未在说明中固定则未记录；本次不补造发布或运行验收。

#### 0.3.0之后的当前源码文档提交

`f3087a19e85da1ef7c0a21bd6e9bf8b2af7591df`（2026-11-02提交日期）补充AAC转MP3原因，前序为tag v0.3.0 / `63336e73c80e897b3b525f6e67e914934f08780c`。固定设计为该HEAD的 `docs/design.md`；不新增软件版本或部署结论。
