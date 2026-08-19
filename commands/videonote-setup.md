---
name: videonote-setup
description: 配置 VideoNote-MCP：体检 / LLM 供应商 / 转写引擎 / 默认值 / B站扫码 / 数据管理
---

# VideoNote-MCP 配置向导

按顺序执行配置检查与引导。**已就绪的步骤直接跳过**，不要重复提问；每步做完汇报结果。

## 0. 前置：确认 MCP 工具已挂载（最重要，不满足则停下）

**本命令的一切配置操作都依赖 videonote 的 MCP 工具**（`health_check` / `get_config` 等，工具名可能带前缀如 `mcp__videonote__*` 或 `mcp__plugin_videonote_videonote__*`，**按工具名判断，不按前缀**）。
先确认本会话里是否挂载了它们。**不要用 CLI / 读配置文件代替 MCP 工具**，也不要自行判断「哪份配置权威」——server 返回的 `data_dir` 才是权威（不要像 diff 数据目录那样去猜）。

- **已挂载** → 继续第 1 步。
- **未挂载** → **立即停止并引导**，不要继续后面的步骤、不要替代性跑 CLI/读文件：
  1. 最可能原因：插件装好后**没重启会话**，插件的 MCP server 尚未加载。请用户**重启 Claude Code 会话**（或 `/reload-plugins`），再重跑本命令。
  2. 若重启后仍没有：让用户在会话里跑 **`/mcp`**，看 `videonote` 是否 Connected、报什么错。**若 `/mcp` 里根本看不到 videonote 条目**（插件虽 enabled 但 server 没注册）→ 说明插件 manifest 的 `mcpServers` 没生效，让用户重装插件再重启：`claude plugin marketplace update videonote` + `claude plugin disable videonote@videonote` + `claude plugin install videonote@videonote`。
  3. 常见报错：uvx 首次从 git 拉包超时（默认 30s）。用 `MCP_TIMEOUT=120000 claude` 重启会话再试。
  4. 可选 CLI 自检（只验证 CLI 可用，**不等同 MCP 工具挂载**）——让用户在终端跑：
     `uvx --from git+https://github.com/HuangYincan/VideoNote-MCP videonote providers list`
- 唯一允许的只读检查：FFmpeg 是否可用（`ffmpeg -version`，`health_check` 也会查，仅作提前确认）。其余一律等 MCP 工具。

## 1. 状态体检

调用 **`health_check`**，看 FFmpeg / 数据库 / 转写引擎 / whisper 模型就绪状态。
- FFmpeg 缺失 → 先让用户安装 FFmpeg（macOS `brew install ffmpeg`，其他平台见官方包源），装完重跑。

## 2. LLM 供应商（API key）

调用 **`get_config()`**，看 `providers` 列表（id / 名称 / key 掩码）与 `app_config.default_provider_id`。
- 已有供应商填了 key → 跳过本步。
- 没有已填 key 的供应商 → **让用户在本会话输入**：
  `! videonote providers list`（看供应商 id）
  `! videonote providers set <provider_id> --api-key '...'`（填 key）
  - API key 为**隐藏输入**，值不经过对话；这一步必须由用户亲手在终端完成，不要试图用其它方式获取 key。
  - 也可以用内置中转站：`! videonote setup`（全屏向导，独立终端更稳）。

## 3. 语音转写引擎

调用 **`get_config()`**，看 `transcriber` 段（引擎 / 模型尺寸 / 预处理 / 说话人分离 / 就绪状态）。
- 本地引擎（fast-whisper / mlx-whisper）模型未下载 → 让用户输 `! videonote transcriber download <size>`。
- 要切换引擎 / 尺寸 → 让用户输 `! videonote transcriber set --engine fast-whisper|groq|bcut|kuaishou|mlx-whisper|funasr [--size <size>]`。
- 云端引擎（groq / bcut / kuaishou）无需下载模型。

## 4. 默认值

展示当前笔记默认值：`get_config()` 的 `app_config` 段（含风格 / 插图 / 视频理解 / 评论弹幕 /
导出格式 / 默认模型；已过滤敏感字段，不要直接 Read 原始 app_config.json，见 #125 C10）。
- 安装时 `/plugin` 的 userConfig 已收过默认值，这里主要是**展示确认**。
- 要修改 → 让用户输 `! videonote setup`（全屏向导，独立终端更稳），或编辑该 JSON。

## 5. B站扫码（可选，需要 AI 字幕 / 评论时）

让用户在本会话输入：
`! videonote login bilibili`
- 二维码会**直接渲染进会话终端**，手机 B 站 App 扫码后自动保存 SESSDATA（进 `downloader.json`）。
- 没配 SESSDATA 时 B 站视频仍可转写（走语音识别），只是拿不到 AI 字幕 / 弹幕评论。

## 6. 数据管理（可选）

- **`list_tasks`** 列全部任务（按语义标题识别）。
- 清理单任务：**`cleanup_note(task_id, include_note?, dry_run=True)`** 先看占用的文件 → `dry_run=False` 再删。
- 全局清理：**`cleanup_all(dry_run=True)`** 先预览 → 确认后 `cleanup_all(include_config?, include_models?)`（默认保留配置与模型）。

---

配置完成后，让用户给一条视频链接验证：`generate_note` 或「AGENT 直接生成」的 `prepare_note_material`。
