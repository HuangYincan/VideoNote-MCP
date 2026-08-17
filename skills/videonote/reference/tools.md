# 工具接口速查 + 配置要点

> 本文件是 SKILL 的参考（非核心）。需要具体工具参数/配置时用 Read 读取。工具签名也可直接从 MCP 工具 schema 获取。

## 生成笔记

### `generate_note(video_url, platform?, quality?, provider_id?, model_name?, format?, style?, screenshot?, link?, video_understanding?, video_interval?, grid_size?, notes_dir?, extras?, include_comments?, comments_limit?)`
- 提交视频，异步生成，返回 `{task_id, status: "PENDING", platform, model_name}`。
- **同视频（`platform:video_id`）再次生成会复用上次转写缓存**（`note_cache`，按引擎/尺寸分键），不再重下+重转写；命中时音频也从缓存复制到新任务，`audio_path` 指向真实文件。`cleanup_all` 会清缓存。
- `quality`: fast / medium / slow。
- `model_name` 省略：用 setup 默认模型，否则供应商第一个可用模型。
- `style`: 9 种（minimal/detailed/academic/tutorial/xiaohongshu/life_journal/task_oriented/business/meeting_minutes）；自定义用 `extras="笔记风格要求：<描述>"`。
- `video_understanding=True` + `video_interval`（默认 6）+ `grid_size`（默认 [3,3]）：视频理解，**需多模态模型**。
- `include_comments=True` + `comments_limit`（默认 20）：整合 B 站弹幕+评论（需 SESSDATA；失败不阻断）。
- `screenshot=True`：插截图，产出便携笔记 note.md + Assets/（相对引用）。**布尔开关与 `format` 双向闭合（#120）**：`screenshot=True` 自动并入 `format`（否则 prompt 不注入标记指令 → LLM 不输出 `*Screenshot-[mm:ss]` → 视频白下载但笔记无图）；`format=["screenshot"]` 等价（即使布尔省略也会下载视频做截图）。`link=True` 同理自动并入 `format`。
- `notes_dir`: 便携笔记目录（指定即写 note.md，即使不插图片；支持 `file://` URI）。
- **并发上限 `VIDEONOTE_MAX_WORKERS`（默认 3）**：超限会拒绝。不要在同一条消息里并行塞多个 `generate_note`（客户端不稳）。`provider_id` 可省略。

### `inspect_video(url, platform?)`
- **只解析、不下载、不提交**。B 站分 P / YouTube 播放列表 / 单集。
- 返回 `{ok, platform, kind: single|multi, title, current_p?, total, truncated, entries:[{p, title, duration, url, video_id}]}`。
- `kind=multi` 时把每条 `entries[].url` 当独立视频，按单集流程（subagent）提交；用户只要一集就用对应那条 url。超过 200 条 `truncated=true`。
- **批量**：多集要全出笔记用 `batch_generate_notes(url, max_entries=10)` 一次排队（服务端逐个提交），省去逐条 subagent。

### `batch_generate_notes(video_url, max_entries=10, quality?, provider_id?, model_name?, format?, style?, screenshot?, extras?, link?, video_understanding?, video_interval?, grid_size?, include_comments?, comments_limit?, notes_dir?)`
- **播放列表/合集/分 P 批量提交**：内部先 `inspect_video` 展开，再逐条提交笔记任务（同一并发门禁，超出 worker 数的排队等待）。高级参数与 `generate_note` 一致（视频理解/弹幕/notes_dir 批量共享同一套设置）。
- 返回 `{ok, total, submitted, truncated?, errors:[{p, title, url, error}], tasks:[{p, title, duration, url, task_id, status}]}`；单条失败不阻断其余，inspect 失败也走同一形状（`total:0, submitted:0, errors:[...]`）。
- 之后逐个 `get_task_status` 轮询（多任务逐个汇报进度，不要同时并行轮询过多）。

### `get_task_status(task_id, include_transcript=False)`
- 轻量快照轮询。返回 `{status, stage, elapsed_secs, message, task_id, result?}`；`stage` 是中文阶段（如「转写中」），`elapsed_secs` 是任务已耗时——轮询汇报可用「转写中，已 3 分钟」。`SUCCESS` 时 `result` 含 `markdown`（或 material 模式的 `frames`/`video_path`/`audio_path`）、`note_dir`、`title`。`note_dir` 指向 `note.md` 所在目录（默认 `{task_id}/gen/`）；生成时指定了 `notes_dir` 会另有 `portable_note_dir` 指向便携副本（`<notes_dir>/<标题>/`）。**默认剥转写**：note 任务要全文用 `include_transcript=True` 或 `get_task_transcript`；material/transcribe 任务的转写是主产物，默认直接返回。
- **默认不含完整转写**——转写可能数万 token，一次工具调用就会撑爆 context。需要转写文本用 `get_task_transcript(task_id)` 按需取；或传 `include_transcript=True` 一次性拿全量（长视频慎用）。

### `get_task_transcript(task_id, segment_range="")`
- 读取已完成任务的**转写文本**（不耗 LLM）。
- **默认只返回前 50 段**（`meta.truncated=true` 时用 `"50-"` 续取或 `"all"` 拿全文）。
- `"0-50"` / `"50-"` / `"150-200"` 按段切片。
- 返回 `{task_id, ok, language, segments, full_text, meta:{total_segments, returned_segments, total_chars, returned_chars, truncated}}`。
- **MCP Resource `videonote://task/{task_id}/transcript`**：转写全文按时间轴渲染的纯文本（含说话人标签），适合整篇直读；工具版用于切片/结构化。

### `wait_for_note`（已废弃）
- **不要调用**。会卡住 MCP 事件循环；现已改为立刻返回当前快照 + `deprecated`。用 `get_task_status` 轮询。

### `cancel_note(task_id)`
- 取消进行中/排队任务（协作式，下一阶段边界生效）；返回 `{ok, task_id, status}`。

## AGENT 直接生成（准备素材）

### `prepare_note_material(video_url, platform?, video_understanding?, video_interval?, grid_size?, include_comments?, comments_limit?)`
- **只准备素材、不调用配置 LLM**：跑下载 → 转写 →（可选）抽帧 →（可选）评论/弹幕，返回素材包（`kind: "material"`）。
- 参数与 `generate_note` 对应；不传 `video_understanding` / `video_interval` / `include_comments` / `comments_limit` 时套 setup 默认（视频理解默认关 / 6s，评论默认关 / 20 条）。
- 返回 `{task_id, status: "PENDING", platform}`；`get_task_status` 轮询到 `SUCCESS` 时 `result` 结构（**素材包契约**：`transcript`/`comments_danmaku` 是主产物，默认直接返回；仅 note 任务才默认剥转写）：
  ```json
  {
    "kind": "material",
    "title": "视频标题",
    "transcript": {
      "language": "zh",
      "full_text": "完整转写全文",
      "segments": [{"start": 0, "end": 5, "text": "..."}]
    },
    "frames": ["file:///绝对/路径/frame_0001.jpg"],
    "comments_danmaku": "【弹幕】…\n【热门评论】…",   // 字符串；无则 null
    "video_path": "/绝对/路径/video.mp4",
    "audio_path": "/绝对/路径/audio.mp3"
  }
  ```
- 用途：**AGENT 直接生成**（agent_direct）—— AGENT 自己读转写、用 Read 看 `frames` 图片、按 `comments_danmaku` 写「观众观点」章节，不经配置 LLM。转写文本优先用 `get_task_transcript(task_id, segment_range=...)` 按需取（超长分段，避免撑爆 context）；评论/弹幕默认已在素材包 result 里（`raw` 恒剥）。

## 模块解耦（独立步骤工具）

流水线各阶段可**独立调用、任意组合**：不想走整条 `generate_note` 时，可只做其中一步，或自己拼素材后交给 `summarize_note`。**素材包**（material dict，见上方 `prepare_note_material` 返回结构）是步骤间传递对象。

### `fetch_subtitles(video_url, platform?)`
- **只取平台字幕**（不下载、不转写），同步返回 `{ok: true, language, full_text, segments}`；无字幕/失败返回 `{ok: false, error}`。
- 适用：先看看平台有没有字幕、只要字幕文本。

### `transcribe_media(file_path)`
- **只做语音识别（ASR）**：给定本地音频/视频文件 → 异步任务，`get_task_status` 轮询到 `SUCCESS` 后 `result` 为 `{kind: "transcript", transcript: {language, full_text, segments}}`。
- 适用：已有音频/视频文件，只想转成文字；不下载、不总结。

### `extract_frames(video_path, video_interval=6, grid_size=[3,3])`
- **只做视频画面理解素材**：给定本地 mp4 → 按间隔抽帧并持久化到 `note_results/{task_id}/frames/`，`result` 为 `{kind: "frames", frames: ["file:///绝对/路径/frame_1.jpg", ...]}`。
- 适用：已有 mp4 只想要关键帧（多模态模型用 Read 看图）。

### `summarize_note(transcript, frames?, comments_danmaku?, title?, style?, extras?, format?, provider_id?, model_name?)`
- **只做 LLM 总结**：吃**素材包**（转写/帧/评论任意组合）→ 异步任务，`result` 为 `{kind: "note", markdown, title}`。
- `comments_danmaku` 传弹幕+评论文本（用 `fetch_comments` + `fetch_danmaku`，没有名为 `fetch_comments_danmaku` 的 MCP 工具）。`provider_id` 可省略。
- 适用：已有字幕/帧/评论，不想重新下载转写，只让 LLM 出笔记。

### 任意组合示例
| 场景 | 组合 |
|------|------|
| 只抓弹幕+评论区观点 | `fetch_comments` / `fetch_danmaku`（独立，不生成笔记） |
| 只做语音识别 | `transcribe_media(音频/视频文件)` |
| 已有字幕 + 画面理解 | `extract_frames(mp4)` + 字幕 → `summarize_note(transcript=字幕, frames=帧)`（不重新下载/转写） |
| 已有 mp4 画面理解 | `extract_frames(mp4)`，或 `generate_note(video_url=mp4, platform="local", video_understanding=True)` |
| 弹幕/评论 + 已有字幕 → 笔记 | `summarize_note(transcript, comments_danmaku=聚合文本, ...)` |

## 多格式导出（机械格式）

### `export_transcript(task_id, formats?, out_dir?)`
- 把已完成任务的转写导出为**确定性格式**（srt/vtt/json），**不耗 LLM**。同步返回。
- `formats` 缺省取 setup 配置的「导出格式默认」（任务成功后也会自动导出这些格式）。
- `out_dir` 缺省 `{task_id}/gen/`；支持 `file://` URI。
- 返回 `{task_id, formats: {fmt: "file://绝对路径"}, errors}`，文件可 Read 直接使用；
  找不到转写时返回 `{ok: False, task_id, error}`（含运行中/失败/已清理的原因，不抛异常）。
- 适用：字幕文件（SRT/VTT）、结构化转写（JSON）、下游程序消费。
- **创意格式**（思维导图/闪卡/LaTeX/typst/用户自定义模板）不在这里——由 Agent 基于
  MD 底稿生成，见 [`output-formats.md`](output-formats.md)。

## 音频增强（多文件合并 / 预处理 / 说话人分离）

### `merge_audio(files, out_dir?)`
- 把多个音频/视频文件合并为一个 16kHz mono wav（FFmpeg concat，自动统一转码）。
- `files`: 至少 2 个本地路径；`out_dir` 缺省数据目录 `note_results/merged/`，均支持 `file://` URI；返回 `{ok, path: "file://绝对路径"}`。
- 适用：多段录音 / 会议分段 / 多个本地视频拼成一段再转写。

### 音频预处理（setup ② 或 `transcriber preprocess on`）
- 转写前先把音频归一化为 16kHz mono wav；超长音频（>1800s）自动分块转写并时间偏移拼接。
- **默认关**（`enable_preprocess`）。开启后 `generate_note` / `transcribe_media` 自动生效。
- 零额外依赖（FFmpeg）；降噪（noisereduce）可选 extras，未装静默降级。

### `diarize_media(audio_file, num_speakers?)`
- 说话人分离（pyannote，**可选重依赖**）：归一化 → 分离 → 返回 `{ok, turns:[{start,end,speaker}]}`。
- **不要传 `hf_token`**（会拒绝）。token 只从 `HUGGINGFACE_HUB_TOKEN` 或 `! videonote setup` 写入的 `app_config.hf_token` 读。
- 需先装 `pyannote.audio` + torch（`uvx --with pyannote.audio --with torch`），并在 huggingface.co 同意 pyannote 模型授权；未装/缺 token 返回带安装指引的 error。
- setup ② 勾选「说话人分离」可引导安装。

## 全自动 / 手动模式

- **默认全自动**：不要先问模式；`generate_note` 不传可选参数即套 setup / userConfig。不要主动问后续优化。
- **手动**：仅当用户说「手动 / 我要选参数」时逐项问（模型、风格、视频理解、评论、截图）。
- **AGENT 直接生成**：仅当用户明确要「你自己写」/`agent_direct`。`agent_direct` 配置键 server 不读，只是编排选择。

## 任务索引与清理

**存储结构**（数据层重构后）：每个任务一个文件夹 `note_results/{task_id}/`，内含：
- `raw/` —— 下载的原始材料（音频/视频/字幕/封面）；
- `gen/` —— 生成材料（`transcript.json` / `note.md` / `Assets/` 截图 / `frames/` 帧 / 导出 srt·vtt·json）；
- `status.json` / `result.json` / `manifest.json` —— 控制文件。

全局索引在 SQLite `video_tasks` 表（含 `title/status/summary/note_dir`）。

### `list_tasks(limit?, offset?)`
- 列出**全部任务**（全局索引，按创建时间倒序），返回 `[{task_id, title, status, summary, platform, created_at, note_dir}]`。
- `limit` / `offset` 可选：分页（缺省全量；任务多时用 `list_tasks(limit=20)` + 递增 offset 翻页）。
- Agent 据此枚举任务、按**语义标题**识别，无需预先知道 task_id。

### `get_task_files(task_id)`
- **先查后清**：列出该任务在磁盘上相关的文件/目录，返回 `{task_id, manifest_paths, existing, meta}`。
- `existing` 含任务文件夹 `raw/` `gen/` 下的真实文件；`meta` 含语义标题/简介。

### `cleanup_note(task_id, include_note=False)`
- 删某任务生成的**中间产物**。
- `include_note=False`（默认）：删 `raw/` + `gen/` 内除 `note.md` 外的一切，**保留最终笔记** + 控制文件；
- `include_note=True`：删整个 `{task_id}/` 文件夹（含 manifest）+ 全局索引记录 + 数据目录内的便携笔记副本。
- **任务仍在运行（或排队中）时拒绝**（返回 `{ok: false, error}`）：先 `cancel_note` 或等终态再清理。
- 以任务文件夹为边界，`resolve()` 校验在数据目录内（防路径穿越）。返回 `{deleted, missing, errors, note_kept, notes_kept_outside}`——`notes_kept_outside` 列出数据目录**外**的便携笔记副本（用户指定 `notes_dir` 时常见）：沙箱红线不删，但路径会列出，不会成无人知晓的孤儿。

### `cleanup_all(include_config=False, include_models=False)`
- **全局清理**（恢复出厂）：清空 `note_results/*`、`static/screenshots/*`、`logs/*` 的所有任务产物 + 全局索引。
- **有进行中/排队任务时拒绝**（返回 `{ok: false, running, running_task_ids, error}`）：先逐个 `cancel_note` 或等全部终态。
- `include_config=False`（默认）：**保留** `config/`（LLM key / cookie / 转写设置）；`include_config=True` 才清。
- `include_models=False`（默认）：**保留** `models/`（已下载模型可复用，重下成本高）；`include_models=True` 才清。

## 供应商 / 模型

- `list_providers()` —— 供应商列表（key 掩码）。空 key 让用户在终端 `videonote providers set <id> --api-key '...'`。
- `add_provider(name, base_url, type)` / `update_provider(provider_id, name?, base_url?, enabled?)` —— 只改非敏感字段；**传 api_key 会被拒绝**。填 key：`! videonote providers set <id> --api-key '...'`。
- `delete_provider(provider_id)` / `delete_model(provider_id, model_name)` —— 删供应商/模型（只清「删的就是默认模型」的默认设置，删非默认不动）。
- `test_provider(provider_id)` —— 用已存 key 探测连接并列出模型（不接受 key 参数）。
- `read_app_config()` —— setup 持久化的默认值（默认供应商/模型、视频理解/弹幕开关、风格、导出格式、notes_dir）；敏感项不返回。
- `list_models(provider_id)` —— `{ok, source, models:[{id, name}]}`。实时 /v1/models，回退本地 DB。
- `add_model(provider_id, model_name)` —— 手动加模型名（接口不可用时）。

## 转写

- `get_transcriber_config()` —— 当前引擎/尺寸/就绪（`ready=false` 时先下载或切云端）。
- `set_transcriber(transcriber_type, whisper_model_size?)` —— 切引擎（fast-whisper/groq/bcut/kuaishou/mlx-whisper/funasr）。
- `list_transcriber_models()` / `download_transcriber_model(model_size, transcriber_type?)` —— 模型管理（下载为后台任务）。
- `set_transcriber("funasr")` —— 中文最优引擎（Paraformer-zh + VAD + 标点）。**可选重依赖**：需 `funasr` + torch（`uvx --with funasr --with torch`）；模型首次转写时自动下载。未装时返回安装指引。

## 其它

- `health_check()` —— `server_version` / ffmpeg / db / 队列 / keyed_providers / `skill_refresh`。
- `preflight(url?, platform?, provider_id?)` —— 提交前体检：ffmpeg / 磁盘剩余 / 转写器就绪 / 供应商 key+模型 / 队列，url 非空时预解析时长。`ok=false` 时先修 detail 再提交，避免长任务半路失败。
- `validate_url(url)` —— 识别平台（bilibili/youtube/douyin/tiktok/kuaishou/local 共 6 种）。内置平台之外返回 `{supported: true, platform: "generic"}`（yt-dlp 通用提取，覆盖 1800+ 站点）；仅当 yt-dlp 也失败时 Agent 接手解析。
- `set_downloader_cookie` —— **拒绝写入 Cookie**。B 站：`! videonote login bilibili` / `! videonote setup`。
- `fetch_comments(video_url, limit=20)` —— B 站热门评论（供生成前预览）。
- `fetch_danmaku(video_url)` —— B 站弹幕汇总（高密度时段 + 高频词）。

## 配置要点

| 场景 | 操作 |
|------|------|
| 配置入口（首次使用） | 用户在 Claude Code 跑 `/videonote-setup`（体检 → 填 key → 转写 → 默认值 → B站扫码 → 数据管理） |
| 给内置供应商填 key | 用户在本会话 `! videonote providers set <id> --api-key 'sk-...'`（隐藏输入、agent 不碰 key） |
| 自建/新增供应商 | `add_provider(name, base_url, type)`（空 key），再 `! videonote providers set <id> --api-key '...'` |
| 查看供应商 / 模型 | `list_providers()`（掩码）/ `list_models(provider_id)` |
| 测试/删除供应商 | `test_provider(provider_id)`；`delete_provider(provider_id)` / `delete_model(provider_id, model_name)` |
| 切本地转写 | `set_transcriber("fast-whisper", "small")` + `download_transcriber_model("small")` |
| 切云端转写 | `set_transcriber("groq")`（groq key 用 CLI 填） |
| B 站登录/AI 字幕/评论 | 用户在本会话 `! videonote login bilibili` 扫码（二维码渲染进会话终端，存 SESSDATA） |
| 本地文件 | `generate_note(video_url="file:///绝对/路径/foo%20bar.mp4")` 或普通路径，`platform` 可省略 |
| 视频理解默认（setup ③） | 用户说「用默认」/ 全自动模式时不传 `video_understanding`/`video_interval` 即套用（默认关/6s） |
| 评论/弹幕整合默认（setup ③） | 用户说「用默认」/ 全自动模式时不传 `include_comments`/`comments_limit` 即套用（默认关/20 条） |
| 笔记默认（setup ③ 新增） | `default_style`（默认 detailed）/ `default_screenshot`（默认关）/ `agent_direct`（默认关，行为与之前一致）；全自动模式不传即套用 |
| 导出格式默认（setup ③ 新增） | `default_export_formats`（srt/vtt/json，默认空）；任务成功后自动导出这些格式，`export_transcript` 不传 formats 时也套用它 |
| 音频预处理（setup ②） | `transcriber preprocess on/off` 或 setup ② 勾选；16kHz 归一 + 超长分块（默认关，零依赖） |
| 说话人分离（setup ②） | `transcriber diarization on/off` 或 setup ② 勾选；pyannote 可选重依赖 + HF_TOKEN + 模型授权 |
| 切中文转写（funasr） | `set_transcriber("funasr")`；需 `uvx --with funasr --with torch`（重依赖可选），模型自动下载 |
| 其他平台（非内置 6 平台） | `validate_url` 返回 `platform:"generic"` → 自动走 yt-dlp 通用提取（覆盖 1800+ 站点） |
| AGENT 直接生成 | `prepare_note_material(video_url, ...)` → 轮询 SUCCESS → 读素材包 → **AGENT 自己写笔记**（不调用配置 LLM） |
