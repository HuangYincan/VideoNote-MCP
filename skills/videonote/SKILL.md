---
name: videonote
description: 用 VideoNote-Mcp 的 MCP 工具把视频链接/本地视频（B站/YouTube/抖音/快手）生成 AI Markdown 笔记。触发词：「生成视频笔记」「视频 → 笔记」「帮我给这个视频做笔记」「从 XX 链接做笔记」。
---

# VideoNote-Mcp —— 视频 → AI 笔记

## ⚡ 强制规则（违反 = 任务失败，不可跳过）

0. **任务开始必须先问「全自动」还是「手动」**：
   - **全自动**：用 setup 默认解析出本次任务的**完整参数清单**，**一次性列出给用户确认**（不逐个问；用户要改再以提问方式改）—— 见规则 2。`generate_note` / `prepare_note_material` 不传这些参数即套默认。
   - **手动**：逐个确认参数（见规则 2）后再调用 `generate_note`。
1. **必须用 MCP 工具**（`generate_note` / `prepare_note_material` / `get_task_status` / `list_providers` / `cancel_note` 等），**不要用 Bash/curl 手工调后端**。唯一例外：让用户在本会话终端输 **`! videonote providers set`**（填 key，隐藏输入、值不过对话）、**`! videonote login bilibili`**（B站扫码）—— 凭证本就该在终端做，`!` 前缀让命令在会话里跑、输出直接进对话。
2. **确认参数依模式而定**：
   - **手动模式**：用户明确指定（或说「你定」）之前，禁止调用 `generate_note` / `prepare_note_material`。必须问：
     - **LLM 模型**：`list_models(provider_id)` 拿到列表 → 呈现给用户选一个；**或选「AGENT 直接生成」**（`agent_direct`：不用配置 LLM、AGENT 自己写笔记，见强制规则 4；用户要则走工作流分支 A）；
     - **笔记风格**：列出真实 9 种让用户选 —— `minimal` 精简 / `detailed` 详细 / `academic` 学术 / `tutorial` 教程 / `xiaohongshu` 小红书 / `life_journal` 生活向 / `task_oriented` 任务导向 / `business` 商业风格 / `meeting_minutes` 会议纪要，或自定义（描述经 `extras` 传入）；
     - **是否视频理解** + 帧间隔秒数（默认 6，需多模态模型）；
     - **是否整合弹幕+评论区观点** + 评论条数（默认 20，需 B 站 SESSDATA，没配引导用户 `! videonote login bilibili`）；
     - **是否插图片** + 笔记保存位置（`notes_dir`）；
   - **全自动模式**：不逐个问，但**先用 setup 默认解析出本次任务将用的完整参数清单，一次性列给用户确认**（每项带默认值。默认来源：`/plugin` 安装时的 userConfig 或 `videonote setup` 向导）：
     1. **生成方式 / LLM 模型**：默认用配置 LLM 的默认模型（`list_providers()` 有 key 的供应商默认模型）；**或改选「AGENT 直接生成」**（`agent_direct`，不走配置 LLM、AGENT 自己写笔记，见规则 4 / 分支 A）；
     2. **笔记风格**：`default_style`（默认 detailed，9 种或自定义）；
     3. **视频理解**：默认关（启用则帧间隔 6s，需多模态模型）；
     4. **弹幕+评论区观点**：默认关（启用则 20 条，需 SESSDATA）；
     5. **插图片 + 保存位置**：`default_screenshot`（默认关；启用则确认 `notes_dir`）；
     6. **生成后是否 AGENT 后续优化**（默认要，见规则 5 精修）。
     - 用户确认「就用这些 / OK」→ 直接按清单生成；
     - 用户要改某项 → **再以提问方式**问该项（如「风格改成 academic 吗？」「这次要不要视频理解？」「生成后还优化吗？」）→ 按新值生成；
     - 用户说「你定」→ 全用清单默认。
3. **单视频一回合一个；多视频用 subagent 并行**：
   - 单视频：一次 `generate_note`（或 `prepare_note_material`）→ 轮询完成 → 呈现。
   - **多视频（>1 个）：主 agent 对每个视频起一个 subagent**，每个 subagent 独立负责「提交 → `get_task_status` 轮询到 SUCCESS → 汇报」；主 agent 汇总呈现。**主 agent 自己绝不在同一回合连续调用多个 `generate_note` / `prepare_note_material`**。
   - 并发上限：最多 `VIDEONOTE_MAX_WORKERS`（默认 3）个进行中任务，超出 server 会拒绝。
4. **AGENT 直接生成（`agent_direct`）—— AGENT 自己写笔记，不调用配置 LLM**：
   1. `prepare_note_material(video_url, video_understanding?, video_interval?, include_comments?, comments_limit?)` → `task_id` → `get_task_status` 轮询到 `SUCCESS`；
   2. 取素材：轮询 SUCCESS 后**轻量结果已含** `frames`（file:// 图片，多模态模型下用 **Read** 看图）、`video_path`、`audio_path`；**转写文本用 `get_task_transcript(task_id)` 按需取**（超长转写用 `segment_range="0-50"` 分段读，避免撑爆 context）；评论/弹幕用 `get_task_status(task_id, include_transcript=True)` 一次性取（连带完整转写，长视频慎用）；
   3. **问笔记风格**（默认 detailed，9 种或自定义）→ **AGENT 自己写 Markdown**；素材包里有 `comments_danmaku` 时，笔记**新增一节「观众观点」**总结观众观点（引用实际内容，不捏造；无可总结写「（无）」）→ 呈现；
   4. 转写可能很长（如 2h 视频超上下文）→ 用 `get_task_transcript(task_id, segment_range="a-b")` 按段分段精修，或让用户指定重点。
5. **生成后是否后续优化**：手动模式**生成后必须问**；全自动模式按参数清单已确认的选择执行（要 → 基于笔记（读 `note_dir` 的 note.md）+ 转写精修：`get_task_transcript(task_id)` 按需取转写、超长分段，从转写挖更多细节、展开讲透、补齐遗漏、修正不一致、增强结构；不要 → 跳过），不再重复问。
6. **平台接手**：`validate_url` / `generate_note` / `prepare_note_material` 返回 `handoff: True` 时，说明该链接不在内置下载器范围（bilibili/youtube/douyin/tiktok/kuaishou/本地文件之外）。**不要重试、不要报错结束**——Agent 接手：用 WebFetch / 浏览器读取页面提取视频源，或用 yt-dlp 通用模式下载，再以本地文件路径调用 `generate_note(video_url="/绝对/路径/x.mp4", platform="local", ...)`。

## 🔧 配置入口（首次使用前）

- **`/videonote-setup`** —— 在 Claude Code 里跑配置向导：体检 → 填 key（`! videonote providers set`）→ 转写引擎/模型下载 → 展示默认值 → B站扫码 → 数据管理。
- **默认值**：`/plugin` 安装时的 userConfig 已收风格/截图/视频理解/评论/转写引擎/笔记位置等默认；要改跑 `! videonote setup` 全屏向导（独立终端更稳）。
- **API key 红线**：key 只经 `! videonote providers set` 输入（隐藏、值不过对话），绝不让用户把 key 贴进对话。

## 工作流

1. **`health_check`** —— ffmpeg/db 就绪；缺失先让用户装 FFmpeg。**若本会话没有 videonote 的 MCP 工具（`mcp__videonote__*`）**：说明插件 MCP server 未加载，先引导用户**重启会话**（或 `/reload-plugins`），不要用 CLI/读文件代替 MCP 工具。
2. **`validate_url(url)`** —— 平台识别；B 站优先用平台字幕（AI 字幕需 SESSDATA）。
3. **`list_providers`** —— 有 key=已填的供应商；没有则让用户在终端配（AGENT 直接生成分支不需要 LLM，但仍需确认已配好）。
4. **问模式 + 确认参数**（见「强制规则 0/2」，问完再继续）。
5. **生成**（按模式与规则 4 选分支）：
   - **分支 A · AGENT 直接生成**（用户要 `agent_direct` 时）：
     1. `prepare_note_material(video_url, video_understanding?, video_interval?, include_comments?, comments_limit?)` → `task_id`；
     2. **轮询**：`get_task_status(task_id)` 轻量快照，直到 `SUCCESS`（长视频可能几分钟；**不要**用阻塞的 `wait_for_note`）；
     3. 取素材 → **AGENT 自己写笔记**（见强制规则 4：`get_task_transcript` 读转写、Read 看 `frames`、问风格、含「观众观点」章节）→ 呈现。
   - **分支 B · 配置 LLM**（默认）：
     1. **`generate_note(video_url, provider_id, model_name=<用户选/默认>, style=<用户选/默认>, ...)`** → `task_id`。
        - 视频理解：`video_understanding=True, video_interval=<秒>`（需多模态模型）；
        - 弹幕评论：`include_comments=True, comments_limit=<条>`；
        - 插图片：`screenshot=True, format=["screenshot"]` + `notes_dir="/用户/给的/路径"`。
     2. **轮询**：`get_task_status(task_id)` 轻量快照，直到 `SUCCESS`（长视频可能几分钟；**不要**用阻塞的 `wait_for_note`）。
     3. **拿到 `result.markdown`** → 直接阅读，用它回答用户的所有问题（无 RAG）；`result.note_dir` 指向笔记文件（读图以它为基准）；追问细节用 **`get_task_transcript(task_id)`** 按需取转写（默认轻量结果不含完整转写）。
6. **呈现笔记**（要点 + 关键章节 + 原文链接）→ **后续优化**（见强制规则 5：手动模式问、全自动模式按清单确认执行；要则读笔记 + 转写/字幕精修，原笔记保留对比）→ 若有多个视频，其余由 subagent 各自处理（见强制规则 3），主 agent 收集结果统一呈现。

## 🖨 输出格式（可选，用户要时）

MD 底稿产出后，用户可能要其他格式。**分工**：机械格式（SRT/VTT/JSON）用 MCP 工具
`export_transcript(task_id, ...)` 直接产出（确定性、不耗 LLM）；思维导图/闪卡/LaTeX/typst/
**用户自定义模板**由 Agent 基于底稿生成。具体步骤见
[`reference/output-formats.md`](reference/output-formats.md)（含 LaTeX 模板选择流程）。

## 🎙 音频增强（可选）

- **多文件合并**：`merge_audio(files, ...)` 把多段录音/会议分段拼成一段再转写。
- **音频预处理**（setup ② 勾选）：16kHz 归一 + 超长分块，转写更稳。
- **说话人分离**：`diarize_media(...)`（pyannote 可选，需 HF_TOKEN + 授权）。
- 细节见 [`reference/tools.md`](reference/tools.md)「音频增强」章节。

## 参考

需要工具参数/配置/故障排查时，用 **Read** 读取同目录 reference/ 下的文件：
- [`reference/tools.md`](reference/tools.md) —— 工具接口速查 + 配置要点（含 `prepare_note_material`）
- [`reference/troubleshooting.md`](reference/troubleshooting.md) —— 故障排查 + 并发/多会话 + B 站细节
