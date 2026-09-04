# 故障排查 + 并发/多会话 + B 站细节

> 本文件是 SKILL 的参考（非核心）。遇到问题时用 Read 读取。

## 故障排查

| 现象 | 处理 |
|------|------|
| `health_check` 显示 `ffmpeg: missing` | 让用户 `brew install ffmpeg`（Linux: `apt install ffmpeg`），装完再跑 |
| `generate_note` 报「需要 provider_id」 | `get_config()` 看内置供应商与默认值；空 key 让用户填：`! videonote providers set <id> --api-key '...'`（MCP 工具拒绝 api_key，填 key 一律走 CLI） |
| 报「供应商还没有可用模型」 | `get_config(provider_id)` 探测（返回 models），或用 CLI `! videonote providers test <id> --default <model>` 探测并设置默认模型 |
| 转写一直失败、提示模型未下载 | 问用户：`videonote transcriber download <size>` 下载，或切云端（`! videonote transcriber set --engine bcut/groq`）—— 不要静默切换 |
| 任务卡在 `INITIALIZING` | 首次使用 fast-whisper 正在下载模型，耐心等；模型大可改用云端转写 |
| 任务 FAILED、message 是 Python 异常原文（如 `missing 1 required positional argument: 'self'` / `TypeError`） | 疑似 #32 同族绑定回归（装饰器误绑实例方法）——仓库已加守卫测试 `tests/test_binding_guard.py` 拦截；升级到最新版（`uvx videonote@latest` 会话自动取新版），仍现则报 issue 附完整 FAILED message |
| B 站下载报 `fatal` / playurl 412 | 已修复（yt-dlp fatal 透传）；仍失败则让用户 `videonote login bilibili`（扫码存 SESSDATA）后重试 |
| 想用 B 站 **AI 字幕**跳过语音识别 | 引导用户跑 `videonote login bilibili`（扫码自动存 SESSDATA）。AI 字幕需登录态；字幕抓取结果在内部处理，任务结果不会暴露 yt-dlp 原始 `raw_info` |
| 小宇宙长时间卡在转写 / 想用官方文稿 | 未配登录态会走本地下载+ASR。引导用户 `! videonote login xiaoyuzhou`（终端扫码，小宇宙 App 确认）后重试；扫不了再用 `--token`。`inspect_video` 应返回 `platform:"xiaoyuzhou"` |
| 小红书下载失败 / 图文笔记 | 图文笔记无法转写。视频遇登录墙/验证码：引导用户 `! videonote login xiaohongshu`（终端扫码，需本机 Chrome/Edge）；扫不了再用 `--cookie`。`inspect_video` 应返回 `platform:"xiaohongshu"` |
| 小红书扫码报 406 | 旧版直连接口签名已失效。请用户升级 videonote 后重试扫码（走本机 Chrome）；或 `! videonote login xiaohongshu --cookie` |
| 整合评论/弹幕时评论拿不到 | 未配 B 站 SESSDATA —— 引导用户 `videonote login bilibili`；抓取失败**不阻断**笔记生成（跳过该部分） |
| 其他平台（非内置平台） | `inspect_video` 返回 `platform:"generic"` → 自动走 **yt-dlp 通用提取**（覆盖 1800+ 站点）；若也失败任务报错 → Agent 接手：WebFetch/浏览器解析视频源后 `generate_note(video_url="/绝对/路径/x.mp4", platform="local")` |
| generic 下载报需登录/JS 渲染 | 该站点 yt-dlp 无法直接提取 —— Agent 用 WebFetch/浏览器处理登录/验证，或让用户 `videonote setup` 向导里配「平台 Cookie」后重试 |
| `process_media(action="diarize")` 报需安装 pyannote / 缺 HF_TOKEN | 引导用户 `transcriber diarization on`（给安装指引 + 存 HF_TOKEN）；pyannote 模型需先在 huggingface.co 同意授权 |
| 视频理解/抽帧报 ffprobe 时长错误 | VideoReader 使用严格 ffprobe（120 秒超时，拒绝无法解析、NaN、Inf、负数）；这是视频抽帧的失败边界。音频预处理的 `probe_duration` 另走 best-effort，失败返回 0，分块时间偏移会回退 1800 秒并记录 warning |
| 提交失败但报错里还有回滚清理信息 | 原始 admission/submit 异常仍是主错误；任务目录、manifest 或 `video_tasks` 清理失败会附加 `errors` / `manifest_error` / `index_error` / `cleanup_error` 诊断，先按诊断处理残留，不要把附加信息误当成新的根因 |
| 任务完成阶段报数据库/索引写入失败 | DAO 会 rollback 后重新抛出；`_save_metadata` 不再吞掉 `video_tasks` 插入失败，因此任务不会伪装成 SUCCESS。检查 SQLite 权限/锁与日志后重试 |
| 转写输出异常（开预处理后） | 预处理默认关；若开了又出问题，`! videonote transcriber preprocess off` 关闭对比 |
| 视频下载 403 / 需会员 | 让用户 `videonote setup` 向导里配「平台 Cookie」（MCP 工具不收 cookie，见安全红线） |
| `generate_note` 报「已有 N 个进行中任务（上限 M）」 | 普通任务 admission 已达上限 —— 提交时已预占名额，覆盖排队与执行全生命周期；等其中一些完成（或 `task(task_id, action="cancel")` 取消）再提交。默认路径合集逐条 `prepare_note_material`（先提交最多 3 个）；后备 LLM 的合集用 `batch_generate_notes`（单次最多 50 条，绕过普通 admission）；互相独立的链接用 subagent |
| `task(action="cancel")` 后仍显示 `CANCELLING` | 这是协作式取消：事件会在字幕、下载、ASR、预处理、ffmpeg、抽帧、说话人分离、B 站弹幕/评论和后处理检查点传播；可控 ffmpeg/下载子进程会尽快退出，但正在进行的 HTTP/模型推理不能硬中断 |
| 想取消 `process_media` | `process_media` 是同步工具，不登记任务注册表，没有可供 `task(action="cancel")` 控制的后台 task_id；只能等待当前 export/merge/diarize 调用自然返回 |

## 并发与多会话

- 每个会话独立起一个 MCP server 进程，任务按 `task_id` 隔离 —— **多个会话可并行生成不同视频的笔记**。
- **普通任务 admission**：本会话内 `generate_note` / `prepare_note_material` 受 `VIDEONOTE_MAX_WORKERS`（默认 3）限制，超出上限会**拒绝**（防止无界排队）。**默认路径**：合集/分 P 每集 `prepare_note_material`（先提交最多 3 个，完成再下一批）；互相独立的链接各一个 **subagent**（提交 → 轮询 → 读素材 → 写笔记 → 汇报）。**后备 LLM**：合集一条 `batch_generate_notes`（单次最多 50 条，绕过普通 admission，由线程池排队）；不要并发调用多个 batch。**主 agent 自己不要在同一回合并行多个 `generate_note` / `prepare_note_material`**。
- **真正突破会话上限**：开多个会话。
- **轮询**：用轻量 `task(task_id)`（默认 action="status"）快照轮询。
- 提交前把计划告诉用户（如「我会依次提交 p10/p11/p12，每个完成后提交下一个」）。
- 资源：whisper/MLX 转写吃 CPU/内存，太多会话并行会卡顿；所有会话共用同一 SQLite，极端并发偶发写冲突。

## B 站细节

- **SESSDATA**：AI 字幕、评论、弹幕的高质量抓取需要登录态。让用户在终端 `videonote login bilibili` 扫码自动获取保存；MCP 工具不收 cookie（安全红线）。
- **字幕优先级**：平台字幕（人工 > AI）> 语音转写。AI 字幕需登录态。
- **弹幕/评论**：`generate_note` 的 `include_comments=True` 内部抓取弹幕（高密度时段 + 高频词）与热门评论（likes 排序）注入笔记 prompt；抓取失败不阻断生成。
