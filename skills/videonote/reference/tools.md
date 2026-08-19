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
- `kind=multi`：用户只要一集 → 直接用对应那条 `entries[].url` 按单集流程提交；要全出 → 用 `batch_generate_notes`（服务端逐个排队，见下）。**不要逐条 subagent 提交**——并发上限 3，逐条会被拒。超过 200 条 `truncated=true`。
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
- `gen/` —— 生成材料（`transcript.json` 转写缓存 / `note.md` / `Assets/` 截图 / `frames/` 帧 / 导出 `transcript.srt`·`transcript.vtt`·`transcript.export.json`）；
- `status.json` / `result.json` / `manifest.json` —— 控制文件。

全局索引在 SQLite `video_tasks` 表（含 `title/status/summary/note_dir`）。

### `list_tasks(limit?, offset?)`
- 列出**全部任务**（全局索引，按创建时间倒序），返回 `[{task_id, title, status, summary, platform, created_at, note_dir}]`。
- `limit` / `offset` 可选：分页（缺省全量；任务多时用 `list_tasks(limit=20)` + 递增 offset 翻页）。
- Agent 据此枚举任务、按**语义标题**识别，无需预先知道 task_id。


### `cleanup_note(task_id, include_note=False)`
- 删某任务生成的**中间产物**。
- `include_note=False`（默认）：删 `raw/` + `gen/` 内除 `note.md` 外的一切，**保留最终笔记** + 控制文件；
- `include_note=True`：删整个 `{task_id}/` 文件夹（含 manifest）+ 全局索引记录 + 数据目录内的便携笔记副本。
- **任务仍在运行（或排队中）时拒绝**（返回 `{ok: false, error}`）：先 `cancel_note` 或等终态再清理。
- 以任务文件夹为边界，`resolve()` 校验在数据目录内（防路径穿越）。返回 `{deleted, missing, errors, note_kept, notes_kept_outside}`——`notes_kept_outside` 列出数据目录**外**的便携笔记副本（用户指定 `notes_dir` 时常见）：沙箱红线不删，但路径会列出，不会成无人知晓的孤儿。

### `cleanup_all(include_config=False, include_models=False, dry_run=False)`
- **全局清理**（恢复出厂）：清空 `note_results/*`、`static/screenshots/*`、`note_cache/*` 的所有任务产物 + 全局索引。**`logs/` 不清**（运行日志不属任务产物）。
- **有进行中/排队任务时拒绝**（返回 `{ok: false, running, running_task_ids, error}`）：先逐个 `cancel_note` 或等全部终态。
- `include_config=False`（默认）：**保留** `config/`（LLM key / cookie / 转写设置）；`include_config=True` 才清。
- `include_models=False`（默认）：**保留** `models/`（已下载模型可复用，重下成本高）；`include_models=True` 才清。

## 配置（只读）

- `get_config(provider_id?)` —— **唯一**配置工具（只读）：`app_config` 默认值（默认供应商/模型、风格、开关，敏感项过滤）+ `providers`（key 掩码）+ `transcriber`（引擎/尺寸/就绪）+ `cookie_configured`（已配 Cookie 的平台名）+ `transcript_source`（固定 `platform_subtitles_first`：平台官方字幕优先，无字幕才转写引擎）。传 `provider_id` 附加该供应商连通性探测（用已存 key，不接受 key 参数）→ `{probe: {ok, models, error}}`。
- **转写素材来源（自动，无需配置）**：`generate_note` / `prepare_note_material` / `batch_generate_notes` 都优先用平台官方字幕（YouTube/B 站人工+自动字幕，YouTube 走 youtube-transcript-api 自动抓取；有官方字幕就不下载音轨、不耗转写引擎）；无字幕或获取失败才下载音频走转写引擎（fast-whisper/groq/funasr 等）。因此「YouTube 有官方字幕」≠「需要转写引擎」——除非视频无字幕。
- **配置修改一律走 CLI**（MCP 面无写配置工具，凭证红线最干净）：
  - 填 key：`! videonote providers set <id> --api-key '...'`（隐藏输入）
  - 新增/删供应商、模型：`! videonote providers add/delete`、`! videonote models ...`
  - 转写引擎/模型下载：`! videonote transcriber set/download`（funasr 中文最优，可选重依赖）

## 其它

- `health_check()` —— `server_version` / ffmpeg / db / 队列 / keyed_providers / `skill_refresh`。
- `preflight(url?, platform?, provider_id?, need_provider=true)` —— 提交前体检：ffmpeg / 磁盘剩余 / 转写器就绪 / 供应商 key+模型 / 队列，url 非空时预解析时长。`ok=false` 时先修 detail 再提交，避免长任务半路失败。`prepare_note_material` 流程不需要 LLM，传 `need_provider=false` 跳过供应商 key/模型检查（#125 C9）。
- `inspect_video(url, platform?)` —— 识别平台 + 检查链接 + 拆多集（#136 并入原 validate_url）。内置平台之外 `platform:"generic"` 走 yt-dlp 通用提取（覆盖 1800+ 站点）；链接无效（空/本地缺失/内网/解析失败）→ `{ok:false, platform?, error}`。
## 配置要点

| 场景 | 操作 |
|------|------|
| 配置入口（首次使用） | 用户在 Claude Code 跑 `/videonote-setup`（体检 → 填 key → 转写 → 默认值 → B站扫码 → 数据管理） |
| 给内置供应商填 key | 用户在本会话 `! videonote providers set <id> --api-key 'sk-...'`（隐藏输入、agent 不碰 key） |
| 查看配置 / 供应商 / 模型 / 转写器 | `get_config()`（只读汇总）；传 `provider_id` 可附加连通性探测 |
| 自建/新增/删除供应商或模型 | `! videonote providers add/delete`、`! videonote models ...`（配置修改一律走 CLI） |
| 切本地转写 | `! videonote transcriber set --engine fast-whisper --size small` + `! videonote transcriber download small` |
| 切云端转写 | `! videonote transcriber set --engine groq`（groq key 用 CLI 填） |
| B 站登录/AI 字幕/评论 | 用户在本会话 `! videonote login bilibili` 扫码（二维码渲染进会话终端，存 SESSDATA） |
| 本地文件 | `generate_note(video_url="file:///绝对/路径/foo%20bar.mp4")` 或普通路径，`platform` 可省略 |
| 视频理解默认（setup ③） | 用户说「用默认」/ 全自动模式时不传 `video_understanding`/`video_interval` 即套用（默认关/6s） |
| 评论/弹幕整合默认（setup ③） | 用户说「用默认」/ 全自动模式时不传 `include_comments`/`comments_limit` 即套用（默认关/20 条） |
| 笔记默认（setup ③ 新增） | `default_style`（默认 detailed）/ `default_screenshot`（默认关）/ `agent_direct`（默认关，行为与之前一致）；全自动模式不传即套用 |
| 导出格式默认（setup ③ 新增） | `default_export_formats`（srt/vtt/json，默认空）；任务成功后自动导出这些格式，`export_transcript` 不传 formats 时也套用它 |
| 音频预处理（setup ②） | `transcriber preprocess on/off` 或 setup ② 勾选；16kHz 归一 + 超长分块（默认关，零依赖） |
| 说话人分离（setup ②） | `transcriber diarization on/off` 或 setup ② 勾选；pyannote 可选重依赖 + HF_TOKEN + 模型授权 |
| 切中文转写（funasr） | `set_transcriber("funasr")`；需 `uvx --with funasr --with torch`（重依赖可选），模型自动下载 |
| 其他平台（非内置 6 平台） | `inspect_video` 返回 `platform:"generic"` → 自动走 yt-dlp 通用提取（覆盖 1800+ 站点） |
| AGENT 直接生成 | `prepare_note_material(video_url, ...)` → 轮询 SUCCESS → 读素材包 → **AGENT 自己写笔记**（不调用配置 LLM） |
