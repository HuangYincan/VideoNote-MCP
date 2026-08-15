# 工具接口速查 + 配置要点

> 本文件是 SKILL 的参考（非核心）。需要具体工具参数/配置时用 Read 读取。工具签名也可直接从 MCP 工具 schema 获取。

## 生成笔记

### `generate_note(video_url, platform?, quality?, provider_id?, model_name?, format?, style?, screenshot?, link?, video_understanding?, video_interval?, grid_size?, notes_dir?, extras?, include_comments?, comments_limit?)`
- 提交视频，异步生成，返回 `{task_id, status: "PENDING", platform, model_name}`。
- `quality`: fast / medium / slow。
- `model_name` 省略：用 setup 默认模型，否则供应商第一个可用模型。
- `style`: 9 种（minimal/detailed/academic/tutorial/xiaohongshu/life_journal/task_oriented/business/meeting_minutes）；自定义用 `extras="笔记风格要求：<描述>"`。
- `video_understanding=True` + `video_interval`（默认 6）+ `grid_size`（默认 [3,3]）：视频理解，**需多模态模型**。
- `include_comments=True` + `comments_limit`（默认 20）：整合 B 站弹幕+评论（需 SESSDATA；失败不阻断）。
- `screenshot=True` + `format=["screenshot"]`：插单张截图，产出便携笔记 note.md + Assets/（相对引用）。
- `notes_dir`: 便携笔记目录（指定即写 note.md，即使不插图片）。
- **任务一次只发一个**：有进行中任务时 server 直接拒绝（先等上一个 SUCCESS/FAILED/CANCELLED）。

### `get_task_status(task_id, include_transcript=False)`
- 轻量快照轮询。返回 `{status, message, task_id, result?}`；`SUCCESS` 时 `result` 含 `markdown`（或 material 模式的 `frames`/`video_path`/`audio_path`）、`note_dir`、`title`。
- **默认不含完整转写**——转写可能数万 token，一次工具调用就会撑爆 context。需要转写文本用 `get_task_transcript(task_id)` 按需取；或传 `include_transcript=True` 一次性拿全量（长视频慎用）。

### `get_task_transcript(task_id, segment_range="")`
- 读取已完成任务的**转写文本**（不耗 LLM，从磁盘按需取，避免撑爆 context）。
- `segment_range` 空（默认）返回完整转写；超长转写按段切片分段读：`"0-50"` 取第 0~49 段、`"50-"` 取第 50 段起、`"150-200"` 取 150~199 段。
- 返回 `{task_id, ok, language, segments, full_text, meta:{total_segments, returned_segments, total_chars, returned_chars, truncated}}`。
- 适用：后续优化精修、回答"视频里某个细节"、Agent 直接生成时读长转写。

### `wait_for_note(task_id, timeout=120, poll_interval=3, include_transcript=False)`
- **阻塞**等 SUCCESS/FAILED/CANCELLED；**多任务/对话中勿用**（会卡住当前轮次）。等完成优先 `get_task_status` 轮询。
- 默认不含完整转写（同 `get_task_status`）；`include_transcript=True` 一次拿全量。

### `cancel_note(task_id)`
- 取消进行中/排队任务（协作式，下一阶段边界生效）；返回 `{ok, task_id, status}`。

## AGENT 直接生成（准备素材）

### `prepare_note_material(video_url, platform?, video_understanding?, video_interval?, grid_size?, include_comments?, comments_limit?)`
- **只准备素材、不调用配置 LLM**：跑下载 → 转写 →（可选）抽帧 →（可选）评论/弹幕，返回素材包（`kind: "material"`）。
- 参数与 `generate_note` 对应；不传 `video_understanding` / `video_interval` / `include_comments` / `comments_limit` 时套 setup 默认（视频理解默认关 / 6s，评论默认关 / 20 条）。
- 返回 `{task_id, status: "PENDING", platform}`；`get_task_status` 轮询到 `SUCCESS` 时 `result` 结构（**默认轻量**：`transcript`/`comments_danmaku` 需 `include_transcript=True` 才有）：
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
- 用途：**AGENT 直接生成**（agent_direct）—— AGENT 自己读转写、用 Read 看 `frames` 图片、按 `comments_danmaku` 写「观众观点」章节，不经配置 LLM。转写文本优先用 `get_task_transcript(task_id, segment_range=...)` 按需取（超长分段，避免撑爆 context）；评论/弹幕用 `get_task_status(task_id, include_transcript=True)` 取。

## 模块解耦（独立步骤工具）

流水线各阶段可**独立调用、任意组合**：不想走整条 `generate_note` 时，可只做其中一步，或自己拼素材后交给 `summarize_note`。**素材包**（material dict，见上方 `prepare_note_material` 返回结构）是步骤间传递对象。

### `fetch_subtitles(video_url, platform?)`
- **只取平台字幕**（不下载、不转写），同步返回 `{language, full_text, segments}`；无字幕/失败返回 `{ok: false, error}`。
- 适用：先看看平台有没有字幕、只要字幕文本。

### `transcribe_media(file_path)`
- **只做语音识别（ASR）**：给定本地音频/视频文件 → 异步任务，`get_task_status` 轮询到 `SUCCESS` 后 `result` 为 `{kind: "transcript", transcript: {language, full_text, segments}}`。
- 适用：已有音频/视频文件，只想转成文字；不下载、不总结。

### `extract_frames(video_path, video_interval=6, grid_size=[3,3])`
- **只做视频画面理解素材**：给定本地 mp4 → 按间隔抽帧并持久化到 `note_results/{task_id}/frames/`，`result` 为 `{kind: "frames", frames: ["file:///绝对/路径/frame_1.jpg", ...]}`。
- 适用：已有 mp4 只想要关键帧（多模态模型用 Read 看图）。

### `summarize_note(transcript, frames?, comments_danmaku?, title?, style?, extras?, format?, provider_id?, model_name?)`
- **只做 LLM 总结**：吃**素材包**（转写/帧/评论任意组合）→ 异步任务，`result` 为 `{kind: "note", markdown, title}`。
- `transcript` 传 `{language, full_text, segments}`；`frames` 传 `extract_frames` 返回的 file:// 路径列表；`comments_danmaku` 传弹幕+评论文本（可用 `fetch_comments` / `fetch_danmaku` 或 `fetch_comments_danmaku` 聚合）。`provider_id` 必填，`model_name` 省略取默认模型。
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
- 返回 `{task_id, formats: {fmt: "file://绝对路径"}, errors}`，文件可 Read 直接使用。
- 适用：字幕文件（SRT/VTT）、结构化转写（JSON）、下游程序消费。
- **创意格式**（思维导图/闪卡/LaTeX/typst/用户自定义模板）不在这里——由 Agent 基于
  MD 底稿生成，见 [`output-formats.md`](output-formats.md)。

## 音频增强（多文件合并 / 预处理 / 说话人分离）

### `merge_audio(files, out_dir?)`
- 把多个音频/视频文件合并为一个 16kHz mono wav（FFmpeg concat，自动统一转码）。
- `files`: 至少 2 个本地路径；返回 `{ok, path: "file://绝对路径"}`。
- 适用：多段录音 / 会议分段 / 多个本地视频拼成一段再转写。

### 音频预处理（setup ② 或 `transcriber preprocess on`）
- 转写前先把音频归一化为 16kHz mono wav；超长音频（>1800s）自动分块转写并时间偏移拼接。
- **默认关**（`enable_preprocess`）。开启后 `generate_note` / `transcribe_media` 自动生效。
- 零额外依赖（FFmpeg）；降噪（noisereduce）可选 extras，未装静默降级。

### `diarize_media(audio_file, num_speakers?, hf_token?)`
- 说话人分离（pyannote，**可选重依赖**）：归一化 → 分离 → 返回 `{ok, turns:[{start,end,speaker}]}`。
- 需先装 `pyannote.audio` + torch（`uvx --with pyannote.audio --with torch`），配 `HF_TOKEN`，
  并在 huggingface.co 同意 pyannote 模型授权；未装/缺 token 返回带安装指引的 error。
- setup ② 勾选「说话人分离」可引导安装。

## 全自动 / 手动模式

- **任务开始必须先问用户**「全自动」还是「手动」。
- **全自动**：用 setup 默认解析出**完整参数清单**（生成方式/LLM 模型（或选 AGENT 直接生成 `agent_direct`）/ `default_style` 默认 detailed / 视频理解默认 / 评论默认 / 截图默认 / **生成后是否后续优化**），**一次性列出给用户确认**、不逐个问；用户确认即生成，要改某项再以提问方式改。「AGENT 直接生成」在选 LLM 模型阶段提供（默认用配置 LLM）。`generate_note` / `prepare_note_material` 不传 style / screenshot / video_understanding / include_comments / agent_direct 即套默认。
- **手动**：逐个确认参数（模型、风格、视频理解、评论/弹幕、截图、是否 AGENT 直接生成），用户明确指定或说「你定」前不调用生成类工具。
- 默认值都可由 setup ③ 覆盖；`agent_direct` 默认关（行为与之前一致，即普通 LLM 生成）。

## 任务索引与清理

**存储结构**（数据层重构后）：每个任务一个文件夹 `note_results/{task_id}/`，内含：
- `raw/` —— 下载的原始材料（音频/视频/字幕/封面）；
- `gen/` —— 生成材料（`transcript.json` / `note.md` / `Assets/` 截图 / `frames/` 帧 / 导出 srt·vtt·json）；
- `status.json` / `result.json` / `manifest.json` —— 控制文件。

全局索引在 SQLite `video_tasks` 表（含 `title/status/summary/note_dir`）。

### `list_tasks()`
- 列出**全部任务**（全局索引，按创建时间倒序），返回 `[{task_id, title, status, summary, platform, created_at, note_dir}]`。
- Agent 据此枚举任务、按**语义标题**识别，无需预先知道 task_id。

### `get_task_files(task_id)`
- **先查后清**：列出该任务在磁盘上相关的文件/目录，返回 `{task_id, manifest_paths, existing, meta}`。
- `existing` 含任务文件夹 `raw/` `gen/` 下的真实文件；`meta` 含语义标题/简介。

### `cleanup_note(task_id, include_note=False)`
- 删某任务生成的**中间产物**。
- `include_note=False`（默认）：删 `raw/` + `gen/` 内除 `note.md` 外的一切，**保留最终笔记** + 控制文件；
- `include_note=True`：删整个 `{task_id}/` 文件夹（含 manifest）+ 全局索引记录。
- 以任务文件夹为边界，`resolve()` 校验在数据目录内（防路径穿越）。返回 `{deleted, missing, errors, note_kept}`。

### `cleanup_all(include_config=False, include_models=False)`
- **全局清理**（恢复出厂）：清空 `note_results/*`、`static/screenshots/*`、`logs/*` 的所有任务产物 + 全局索引。
- `include_config=False`（默认）：**保留** `config/`（LLM key / cookie / 转写设置）；`include_config=True` 才清。
- `include_models=False`（默认）：**保留** `models/`（已下载模型可复用，重下成本高）；`include_models=True` 才清。

## 供应商 / 模型

- `list_providers()` —— 供应商列表（key 掩码）。空 key 让用户在终端 `videonote providers set <id> --api-key '...'`。
- `add_provider(name, api_key, base_url, type)` / `update_provider(provider_id, ...)` —— 新增/更新（**填 key 建议走 CLI，不进对话**）。
- `list_models(provider_id)` —— 实时 /v1/models，回退本地 DB。
- `add_model(provider_id, model_name)` —— 手动加模型名（接口不可用时）。

## 转写

- `get_transcriber_config()` —— 当前引擎/尺寸/就绪（`ready=false` 时先下载或切云端）。
- `set_transcriber(transcriber_type, whisper_model_size?)` —— 切引擎（fast-whisper/groq/bcut/kuaishou/mlx-whisper）。
- `list_transcriber_models()` / `download_transcriber_model(model_size, transcriber_type?)` —— 模型管理（下载为后台任务）。
- `set_transcriber("funasr")` —— 中文最优引擎（Paraformer-zh + VAD + 标点）。**可选重依赖**：需 `funasr` + torch（`uvx --with funasr --with torch`）；模型首次转写时自动下载。未装时返回安装指引。

## 其它

- `health_check()` —— ffmpeg/db/whisper 就绪状态。
- `validate_url(url)` —— 识别平台（bilibili/youtube/douyin/tiktok/kuaishou/local）。内置 5 平台之外返回 `{supported: true, platform: "generic"}`（yt-dlp 通用提取，覆盖 1800+ 站点）；仅当 yt-dlp 也失败时 Agent 接手解析。
- `set_downloader_cookie(platform, cookie)` —— 设置平台 Cookie（如 B 站 `SESSDATA=...`）。
- `fetch_comments(video_url, limit=20)` —— B 站热门评论（供生成前预览）。
- `fetch_danmaku(video_url)` —— B 站弹幕汇总（高密度时段 + 高频词）。

## 配置要点

| 场景 | 操作 |
|------|------|
| 配置入口（首次使用） | 用户在 Claude Code 跑 `/videonote-setup`（体检 → 填 key → 转写 → 默认值 → B站扫码 → 数据管理） |
| 给内置供应商填 key | 用户在本会话 `! videonote providers set <id> --api-key 'sk-...'`（隐藏输入、agent 不碰 key） |
| 自建/新增供应商 | `add_provider(name, api_key, base_url, type)` |
| 查看供应商 / 模型 | `list_providers()`（掩码）/ `list_models(provider_id)` |
| 切本地转写 | `set_transcriber("fast-whisper", "small")` + `download_transcriber_model("small")` |
| 切云端转写 | `set_transcriber("groq")`（groq key 用 CLI 填） |
| B 站登录/AI 字幕/评论 | 用户在本会话 `! videonote login bilibili` 扫码（二维码渲染进会话终端，存 SESSDATA）；或 `set_downloader_cookie(platform="bilibili", cookie="SESSDATA=...")` |
| 本地文件 | `generate_note(video_url="/绝对/路径/x.mp4", platform="local", ...)` |
| 视频理解默认（setup ③） | 用户说「用默认」/ 全自动模式时不传 `video_understanding`/`video_interval` 即套用（默认关/6s） |
| 评论/弹幕整合默认（setup ③） | 用户说「用默认」/ 全自动模式时不传 `include_comments`/`comments_limit` 即套用（默认关/20 条） |
| 笔记默认（setup ③ 新增） | `default_style`（默认 detailed）/ `default_screenshot`（默认关）/ `agent_direct`（默认关，行为与之前一致）；全自动模式不传即套用 |
| 导出格式默认（setup ③ 新增） | `default_export_formats`（srt/vtt/json，默认空）；任务成功后自动导出这些格式，`export_transcript` 不传 formats 时也套用它 |
| 音频预处理（setup ②） | `transcriber preprocess on/off` 或 setup ② 勾选；16kHz 归一 + 超长分块（默认关，零依赖） |
| 说话人分离（setup ②） | `transcriber diarization on/off` 或 setup ② 勾选；pyannote 可选重依赖 + HF_TOKEN + 模型授权 |
| 切中文转写（funasr） | `set_transcriber("funasr")`；需 `uvx --with funasr --with torch`（重依赖可选），模型自动下载 |
| 其他平台（非内置 5 平台） | `validate_url` 返回 `platform:"generic"` → 自动走 yt-dlp 通用提取（覆盖 1800+ 站点） |
| AGENT 直接生成 | `prepare_note_material(video_url, ...)` → 轮询 SUCCESS → 读素材包 → **AGENT 自己写笔记**（不调用配置 LLM） |
