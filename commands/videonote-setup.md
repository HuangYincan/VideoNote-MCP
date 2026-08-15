---
name: videonote-setup
description: 配置 VideoNote-MCP：体检 / LLM 供应商 / 转写引擎 / 默认值 / B站扫码 / 数据管理
---

# VideoNote-MCP 配置向导

按顺序执行配置检查与引导。**已就绪的步骤直接跳过**，不要重复提问；每步做完汇报结果。

## 1. 状态体检

调用 **`health_check`**，看 FFmpeg / 数据库 / 转写引擎 / whisper 模型就绪状态。
- FFmpeg 缺失 → 先让用户安装 FFmpeg（macOS `brew install ffmpeg`，其他平台见官方包源），装完重跑。

## 2. LLM 供应商（API key）

调用 **`list_providers`**，列出已配置供应商（id / 名称 / key 是否已填）。
- 已有供应商填了 key → 跳过本步。
- 没有已填 key 的供应商 → **让用户在本会话输入**：
  `! videonote providers list`（看供应商 id）
  `! videonote providers set <provider_id> --api-key '...'`（填 key）
  - API key 为**隐藏输入**，值不经过对话；这一步必须由用户亲手在终端完成，不要试图用其它方式获取 key。
  - 也可以用内置中转站：`! videonote setup`（全屏向导，独立终端更稳）。

## 3. 语音转写引擎

调用 **`get_transcriber_config`**，看当前引擎 / 模型尺寸 / 预处理 / 说话人分离。
- 本地引擎（fast-whisper / mlx-whisper）模型未下载 → 调 **`download_transcriber_model(model_size, transcriber_type)`** 后台下载，用 `list_transcriber_models` 查进度；或让用户输 `! videonote transcriber download <size>`。
- 要切换引擎 / 尺寸 → 调 **`set_transcriber(...)`**（fast-whisper / groq / bcut / kuaishou / mlx-whisper）。
- 云端引擎（groq / bcut / kuaishou）无需下载模型。

## 4. 默认值

展示当前笔记默认值：**Read** `{health_check 返回的 data_dir}/config/app_config.json`
（含风格 / 插图 / 视频理解 / 评论弹幕 / 导出格式 / 默认模型）。
- 安装时 `/plugin` 的 userConfig 已收过默认值，这里主要是**展示确认**。
- 要修改 → 让用户输 `! videonote setup`（全屏向导，独立终端更稳），或编辑该 JSON。

## 5. B站扫码（可选，需要 AI 字幕 / 评论时）

让用户在本会话输入：
`! videonote login bilibili`
- 二维码会**直接渲染进会话终端**，手机 B 站 App 扫码后自动保存 SESSDATA（进 `downloader.json`）。
- 没配 SESSDATA 时 B 站视频仍可转写（走语音识别），只是拿不到 AI 字幕 / 弹幕评论。

## 6. 数据管理（可选）

- **`list_tasks`** 列全部任务（按语义标题识别）。
- 清理单任务：**`get_task_files(task_id)`** 先看占用的文件 → **`cleanup_note(task_id, include_note?)`**。
- 全局清理：**`cleanup_all(include_config?, include_models?)`**（默认保留配置与模型）。

---

配置完成后，让用户给一条视频链接验证：`generate_note` 或「AGENT 直接生成」的 `prepare_note_material`。
