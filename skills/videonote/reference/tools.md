# 工具接口速查 + 配置要点

> 本文件是 SKILL 的参考（非核心）。需要具体工具参数/配置时用 Read 读取。工具签名也可直接从 MCP 工具 schema 获取。

## 生成笔记

### `generate_note(video_url, platform?, quality?, provider_id?, model_name?, format?, style?, screenshot?, link?, video_understanding?, video_interval?, grid_size?, notes_dir?, extras?, include_comments?, comments_limit?)`
- 提交视频，异步生成，返回 `{task_id, status: "PENDING", platform, model_name}`。
- **同视频（`platform:video_id`）再次生成会复用上次转写缓存**（`note_cache`，按引擎/尺寸分键），不再重下+重转写；命中时音频也从缓存复制到新任务，`audio_path` 指向真实文件。`cleanup`（全局清理）会清缓存。
- `quality`: fast / medium / slow。
- `model_name` 省略：用 setup 默认模型，否则供应商第一个可用模型。
- `style`: 9 种（minimal/detailed/academic/tutorial/xiaohongshu/life_journal/task_oriented/business/meeting_minutes）；自定义用 `extras="笔记风格要求：<描述>"`。
- `video_understanding=True` + `video_interval`（默认 6）+ `grid_size`（默认 [3,3]）：视频理解，**需多模态模型**。
- `include_comments=True` + `comments_limit`（默认 20）：整合 B 站弹幕+评论（需 SESSDATA；失败不阻断）。
- `screenshot=True`：插截图，产出便携笔记 note.md + Assets/（相对引用）。**布尔开关与 `format` 双向闭合（#120）**：`screenshot=True` 自动并入 `format`（否则 prompt 不注入标记指令 → LLM 不输出 `*Screenshot-[mm:ss]` → 视频白下载但笔记无图）；`format=["screenshot"]` 等价（即使布尔省略也会下载视频做截图）。`link=True` 同理自动并入 `format`。
- `notes_dir`: 便携笔记目录（指定即写 note.md，即使不插图片；支持 `file://` URI）。**安全边界**：数据目录外（含 env `VIDEONOTE_NOTES_DIR` 兜底）默认拒绝，需 `VIDEONOTE_ALLOW_EXTERNAL_PATHS=1`/插件 `allow_external_paths` 放行（#142）——报错即说明放行方式，转告用户即可。
- **并发上限 `VIDEONOTE_MAX_WORKERS`（默认 3）**：超限会拒绝。不要在同一条消息里并行塞多个 `generate_note`（客户端不稳）。`provider_id` 可省略。

### `inspect_video(url, platform?)`
- **只解析、不下载、不提交**。B 站分 P / YouTube 播放列表 / 单集。
- 返回 `{ok, platform, kind: single|multi, title, current_p?, total, truncated, entries:[{p, title, duration, url, video_id}]}`。
- `kind=multi`：用户只要一集 → 直接用对应那条 `entries[].url` 按单集流程提交；要全出 → 用 `batch_generate_notes`（服务端逐个排队，见下）。**不要逐条 subagent 提交**——并发上限 3，逐条会被拒。超过 200 条 `truncated=true`。
- **批量**：多集要全出笔记用 `batch_generate_notes(url, max_entries=10)` 一次排队（服务端逐个提交），省去逐条 subagent。
- 内置平台之外 `platform:"generic"` 走 yt-dlp 通用提取（覆盖 1800+ 站点）；链接无效（空/本地缺失/内网/解析失败）→ `{ok:false, platform?, error}`。

### `batch_generate_notes(video_url, max_entries=10, quality?, provider_id?, model_name?, format?, style?, screenshot?, extras?, link?, video_understanding?, video_interval?, grid_size?, include_comments?, comments_limit?, notes_dir?)`
- **播放列表/合集/分 P 批量提交**：内部先 `inspect_video` 展开，再逐条提交笔记任务（同一并发门禁，超出 worker 数的排队等待）。高级参数与 `generate_note` 一致（视频理解/弹幕/notes_dir 批量共享同一套设置）。
- 返回 `{ok, total, submitted, truncated?, errors:[{p, title, url, error}], tasks:[{p, title, duration, url, task_id, status}]}`；单条失败不阻断其余，inspect 失败也走同一形状（`total:0, submitted:0, errors:[...]`）。
- 之后逐个 `task(task_id)` 轮询（多任务逐个汇报进度，不要同时并行轮询过多）。

### `task(task_id, action="status", segment_range="")`
- 任务控制面（#138）：查询 / 取转写 / 取消，一个入口三分支。`segment_range` 仅 transcript 分支生效，其余分支忽略；返回结构随 action 不同。
- **`action="status"`（默认）**：轻量快照轮询。返回 `{status, stage, elapsed_secs, message, task_id, result?}`；`stage` 是中文阶段（如「转写中」），`elapsed_secs` 是任务已耗时——轮询汇报可用「转写中，已 3 分钟」。`SUCCESS` 时 `result` 含 `markdown`（或 material 模式的 `frames`/`video_path`/`audio_path`）、`note_dir`、`title`。`note_dir` 指向 `note.md` 所在目录（默认 `{task_id}/gen/`）；生成时指定了 `notes_dir` 会另有 `portable_note_dir` 指向便携副本（`<notes_dir>/<标题>/`）。
  - **默认剥转写**——转写可能数万 token，一次工具调用就会撑爆 context。note 任务要全文走 `task(task_id, action="transcript", segment_range="all")`；material 任务的转写是主产物，默认直接返回。
- **`action="transcript"`**：读取已完成任务的**转写文本**（不耗 LLM），按需分段取。`segment_range` 空（默认）只返回前 50 段（`meta.truncated=true` 时用 `"50-"` 续取或 `"all"` 拿全文）；`"0-50"` / `"50-"` / `"150-200"` 按段切片。返回 `{task_id, ok, language, segments, full_text, meta:{total_segments, returned_segments, total_chars, returned_chars, truncated}}`。任务未成功/无转写时 `ok:false`。
  - **MCP Resource `videonote://task/{task_id}/transcript`**：转写全文按时间轴渲染的纯文本（含说话人标签），适合整篇直读；工具版用于切片/结构化。
- **`action="cancel"`**：取消进行中/排队任务（协作式，下一阶段边界生效，LLM 总结时每 chunk 检查）；返回 `{ok, task_id, status, message?}`。

## AGENT 直接生成（准备素材）

### `prepare_note_material(video_url, platform?, video_understanding?, video_interval?, grid_size?, include_comments?, comments_limit?)`
- **只准备素材、不调用配置 LLM**：跑下载 → 转写 →（可选）抽帧 →（可选）评论/弹幕，返回素材包（`kind: "material"`）。
- 参数与 `generate_note` 对应；不传 `video_understanding` / `video_interval` / `include_comments` / `comments_limit` 时套 setup 默认（视频理解默认关 / 6s，评论默认关 / 20 条）。
- 返回 `{task_id, status: "PENDING", platform}`；`task(task_id)` 轮询到 `SUCCESS` 时 `result` 结构（**素材包契约**：`transcript`/`comments_danmaku` 是主产物，默认直接返回；仅 note 任务才默认剥转写）：
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
- 用途：**AGENT 直接生成**（agent_direct）—— AGENT 自己读转写、用 Read 看 `frames` 图片、按 `comments_danmaku` 写「观众观点」章节，不经配置 LLM。转写文本优先用 `task(task_id, action="transcript", segment_range=...)` 按需取（超长分段，避免撑爆 context）；评论/弹幕默认已在素材包 result 里（`raw` 恒剥）。

## 媒体加工（导出 / 合并 / 说话人分离）

### `process_media(action="export", task_id?, formats?, out_dir?, files?, audio_file?, num_speakers?, hf_token?)`
- 媒体/转写加工（#138）：三个分支共用入口，参数按 action 分支生效（如 `formats` 仅 export 用）；分支缺参各自显式报错（export 缺 `task_id` / merge 缺 `files` / diarize 缺 `audio_file` → ValueError）。
- **`action="export"`（默认）**：把已完成任务的转写导出为**确定性格式**（srt/vtt/json），**不耗 LLM**。同步返回。
  - `formats` 缺省取 setup 配置的「导出格式默认」（任务成功后也会自动导出这些格式）。
  - `out_dir` 缺省 `{task_id}/gen/`；支持 `file://` URI。
  - 返回 `{ok, task_id, formats: {fmt: "file://绝对路径"}, errors: {}}`，文件可 Read 直接使用；找不到转写时返回 `{ok: False, task_id, error}`（含运行中/失败/已清理的原因，不抛异常）。
  - 适用：字幕文件（SRT/VTT）、结构化转写（JSON）、下游程序消费。
  - **创意格式**（思维导图/闪卡/LaTeX/typst/用户自定义模板）不在这里——由 Agent 基于 MD 底稿生成，见 [`output-formats.md`](output-formats.md)。
- **`action="merge"`**：把多个音频/视频文件合并为一个 16kHz mono wav（FFmpeg concat，自动统一转码）。
  - `files`: 至少 2 个本地路径（编码可不同）；`out_dir` 缺省数据目录 `note_results/merged/`，均支持 `file://` URI。
  - **数据目录外的本地输入/输出默认拒绝**（#142 边界），需 `VIDEONOTE_ALLOW_EXTERNAL_PATHS=1` 放行（报错即说明放行方式）。
  - 返回 `{ok, path: "file://绝对路径"}` 或 `{ok: false, error}`。适用：多段录音 / 会议分段 / 多个本地视频拼成一段再转写。
- **`action="diarize"`**：说话人分离（pyannote，**可选重依赖**）：归一化 → 分离 → 返回 `{ok, turns:[{speaker,start,end}], num_speakers}` 或 `{ok: false, error}`。
  - `audio_file` 必填（本地音频/视频文件，自动归一化）；`num_speakers` 可选（缺省自动检测）。
  - **不要传 `hf_token`**（会拒绝——蜜罐参数，与 action 无关，防切换 action 绕过凭证红线）。token 只从 `HUGGINGFACE_HUB_TOKEN` 或 `! videonote setup` 写入的 `app_config.hf_token` 读。
  - 需先装 `pyannote.audio` + torch（`uvx --with pyannote.audio --with torch`），并在 huggingface.co 同意 pyannote 模型授权；未装/缺 token 返回带安装指引的 error。
  - setup ② 勾选「说话人分离」可引导安装。

### 音频预处理（setup ② 或 `transcriber preprocess on`）
- 转写前先把音频归一化为 16kHz mono wav；超长音频（>1800s）自动分块转写并时间偏移拼接。
- **默认关**（`enable_preprocess`）。开启后 `generate_note` / `prepare_note_material` 自动生效。
- 零额外依赖（FFmpeg）；降噪（noisereduce）可选 extras，未装静默降级。

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

### `cleanup(task_id?, dry_run?, include_note?, include_config?, include_models?)`
- 清理任务产物（#138）：`task_id` 非空 = 单任务清理；为空 = 全局清理（恢复出厂）。参数冲突显式报错：单任务模式传 `include_config`/`include_models`、全局模式传 `include_note` 都会 ValueError（避免静默忽略误导）。
- **`task_id` 非空（单任务）**：删该任务生成的**中间产物**（下载的视频/音频、转写、截图、临时文件、dl 目录等）。
  - `include_note=False`（默认）：删 `raw/` + `gen/` 内除 `note.md` 外的一切，**保留最终笔记** + 控制文件；
  - `include_note=True`：删整个 `{task_id}/` 文件夹（含 manifest）+ 全局索引 `video_tasks` 该任务记录（否则 `list_tasks` 出现 note_dir 悬空的任务）+ 数据目录内的便携笔记副本。
- **`task_id` 为空（全局）**：清空 `note_results/*`、`static/screenshots/*`、`note_cache/*` 的所有任务产物 + 全局索引。**`logs/` 不清**（运行日志不属任务产物，#121 C3）。
  - `include_config=False`（默认）：**保留** `config/`（LLM key / cookie / 转写设置）；`include_config=True` 才清。
  - `include_models=False`（默认）：**保留** `models/`（已下载模型可复用，重下成本高）；`include_models=True` 才清。
  - **`include_config` / `include_models` 默认拒绝执行**（#142：删凭据/模型不可逆），需 `VIDEONOTE_ALLOW_DESTRUCTIVE_CLEANUP=1`/插件 `allow_destructive_cleanup` 放行；未放行时 dry_run 把它标注为「将拒绝清理」，直接执行返回 `{ok: false, error}`。
- **`dry_run=True`：先查后清**——单任务列出该任务占用的文件，全局预览 `would_clean`/`would_keep`/`running`，都不删任何东西；确认后再去掉 `dry_run` 执行。
- **任务仍在运行（或排队中）时拒绝**（单任务返回 `{ok: false, error}`；全局返回 `{ok: false, running, running_task_ids, error}`）：先 `task(task_id, action="cancel")` 或等终态再清理。`include_models=True` 且仍有模型在后台下载时也拒绝（删 `models/` 会打断下载线程，#123 A1）。
- 以任务文件夹为边界，`resolve()` 校验在数据目录内（防路径穿越）。返回 `{deleted, missing, errors, note_kept, notes_kept_outside}`——`notes_kept_outside` 列出数据目录**外**的便携笔记副本（用户指定 `notes_dir` 时常见）：沙箱红线不删，但路径会列出，不会成无人知晓的孤儿。

## 配置（只读）

- `get_config(provider_id?)` —— **唯一**配置工具（只读）：`app_config` 默认值（默认供应商/模型、风格、开关，敏感项过滤）+ `providers`（key 掩码）+ `transcriber`（引擎/尺寸/就绪）+ `cookie_configured`（已配 Cookie 的平台名）+ `transcript_source`（固定 `platform_subtitles_first`：平台官方字幕优先，无字幕才转写引擎）。传 `provider_id` 附加该供应商连通性探测（用已存 key，不接受 key 参数）→ `{probe: {ok, models, error}}`。
- **转写素材来源（自动，无需配置）**：`generate_note` / `prepare_note_material` / `batch_generate_notes` 都优先用平台官方字幕（YouTube/B 站人工+自动字幕，小宇宙官方文稿；YouTube 走 youtube-transcript-api；小宇宙走 `episode-transcript/get`，需 `! videonote login xiaoyuzhou`）。有官方字幕就不下载音轨、不耗转写引擎；无字幕或获取失败才下载音频走转写引擎（fast-whisper/groq/funasr 等）。因此「有官方字幕」≠「需要转写引擎」。
- **配置修改一律走 CLI**（MCP 面无写配置工具，凭证红线最干净）：
  - 填 key：`! videonote providers set <id> --api-key '...'`（隐藏输入）
  - 新增/删供应商、模型：`! videonote providers add/delete`、`! videonote models ...`
  - 转写引擎/模型下载：`! videonote transcriber set/download`（funasr 中文最优，可选重依赖）

## 体检（提交前）

### `health_check(need_provider=true, provider_id?, url?, platform?)`
- 体检（#138）：检查 MCP 运行环境与提交前就绪状态，`checks` 数组逐项给出 `{name, ok, detail}`。
- 检查项：ffmpeg / 数据库 / 磁盘剩余 / 转写器就绪（本地模型已下载/云端 key）/ 供应商 key 与模型（仅 `need_provider=True`）/ 任务队列；`url` 非空时顺带预解析视频时长（仅参考，不拦截），返回额外 `duration_secs`。
- 返回 `{ok, server_version, plugin_version, whisper_models, engine_advice, audio_enhance, keyed_providers, queue_length, max_workers, data_dir, skill_refresh, checks, duration_secs?}`。`ok=false` 时先解决 `detail` 里的问题再提交，避免长任务跑到半路才因模型未下载 / 磁盘满失败。
- **`need_provider` 默认 True**（generate_note 需要 LLM 供应商）。只做素材包（prepare_note_material 不调 LLM）时传 False，跳过供应商 key/模型检查——否则会得到「无已填 key 的供应商」的误导结论（#124 A12）。

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
| 小宇宙官方文稿 | 用户在本会话 `! videonote login xiaoyuzhou` 用小宇宙 App 扫码（或 `--token` 粘贴）；未登录会回退本地下载+ASR，长节目会非常慢 |
| 本地文件 | `generate_note(video_url="file:///绝对/路径/foo%20bar.mp4")` 或普通路径，`platform` 可省略 |
| 视频理解默认（setup ③） | 用户说「用默认」/ 全自动模式时不传 `video_understanding`/`video_interval` 即套用（默认关/6s） |
| 评论/弹幕整合默认（setup ③） | 用户说「用默认」/ 全自动模式时不传 `include_comments`/`comments_limit` 即套用（默认关/20 条） |
| 笔记默认（setup ③ 新增） | `default_style`（默认 detailed）/ `default_screenshot`（默认关）/ `agent_direct`（默认关，行为与之前一致）；全自动模式不传即套用 |
| 导出格式默认（setup ③ 新增） | `default_export_formats`（srt/vtt/json，默认空）；任务成功后自动导出这些格式，`process_media(action="export")` 不传 formats 时也套用它 |
| 音频预处理（setup ②） | `transcriber preprocess on/off` 或 setup ② 勾选；16kHz 归一 + 超长分块（默认关，零依赖） |
| 说话人分离（setup ②） | `transcriber diarization on/off` 或 setup ② 勾选；pyannote 可选重依赖 + HF_TOKEN + 模型授权 |
| 切中文转写（funasr） | `set_transcriber("funasr")`；需 `uvx --with funasr --with torch`（重依赖可选），模型自动下载 |
| 其他平台（非内置平台） | `inspect_video` 返回 `platform:"generic"` → 自动走 yt-dlp 通用提取（覆盖 1800+ 站点） |
| AGENT 直接生成 | `prepare_note_material(video_url, ...)` → 轮询 SUCCESS → 读素材包 → **AGENT 自己写笔记**（不调用配置 LLM） |
