---
name: videonote
description: 用 VideoNote-Mcp 的 MCP 工具把视频链接/本地视频（B站/YouTube/抖音/快手/小宇宙）生成 AI Markdown 笔记。触发词：「生成视频笔记」「视频 → 笔记」「帮我给这个视频做笔记」「从 XX 链接做笔记」。
---

# VideoNote-Mcp —— 视频 → AI 笔记

## 默认路径（给个链接就跑）

用户没说「手动 / 我要选参数 / AGENT 自己写」时，**不要先问全自动还是手动，不要列 6 项等确认**：

1. 没有 `mcp__videonote__*` → 停，让用户重启会话 / `/reload-plugins`。不要用 CLI 代替 MCP。
2. `health_check`：缺 ffmpeg 先让用户装。`keyed_providers=0` 且用户要 AI 笔记 → 让用户 `! videonote providers set`（key 不进对话）。
3. `inspect_video(url)`（识别平台 + 检查链接 + 拆多集；链接无效会直接给原因）。
4. `kind=multi`（合集 / 分 P / 播放列表）→ 一条 `batch_generate_notes` 全出笔记。
   - `kind=single`：一条 url。
   - `kind=multi`：每条 `entries[].url` 当独立视频。用户没说「只要第 N 集」就处理全部（太多先报 `total`，问要不要只跑 `current_p`）。
     要全出笔记 → 一条 `batch_generate_notes(url)` 服务端逐个排队（省去逐条 subagent，见规则 2）。
5. 长视频 / 首次使用 / 最近改过转写引擎 → `health_check(url)` 体检（ffmpeg/磁盘/转写器/供应商 key，`ok=false` 先修）。
6. 直接 `generate_note(video_url)`（`provider_id` 可省略）。参数不传 = setup / userConfig 默认。
   - **转写素材自动优先平台官方字幕**（YouTube/B 站人工+自动字幕 / 小宇宙官方文稿）——有官方字幕的视频不会走本地转写引擎；无字幕/获取失败才下载音轨转写。用户问「为什么走/没走转写引擎」先看该视频有无官方字幕。小宇宙官方文稿需用户先 `! videonote login xiaoyuzhou`。
7. **`task(task_id)` 轮询**到 SUCCESS / FAILED / CANCELLED。等待中可用 `stage` / `elapsed_secs` 报进度（如「转写中，已 3 分钟」）。
8. 呈现 `result.markdown`（要点 + 章节 + 原文链接）。用户要细节再用 `task(task_id, action="transcript")`（默认前 50 段；全文 `segment_range="all"`）。
9. **不要主动问「要不要后续优化」**。用户要精修再读笔记 + 转写改。

用户改某一项（风格 / 视频理解 / 评论 / 截图）→ 只改那一项再跑。用户说「手动」才逐项问（见 reference）。

## 强制规则

1. **必须用 MCP 工具**。凭证例外：本会话 `! videonote providers set`、`! videonote login bilibili`、`! videonote login xiaoyuzhou`。
2. **单视频一回合一个提交**。**合集 / 分 P / 播放列表**（inspect_video `kind=multi`）：一条 `batch_generate_notes` 服务端展开+排队（同并发门禁，超出 worker 数的排队等待）。**互相独立的多个链接**：每个 url 一个 **subagent**（提交 → 轮询 → 汇报），主 agent 汇总。不要在同一条消息里并行多个 `generate_note`。上限 `VIDEONOTE_MAX_WORKERS`（默认 3）。
3. **AGENT 自己写笔记**（用户明确要 `agent_direct` / 「你自己写」）：`prepare_note_material` → 轮询 → `task(task_id, action="transcript")` + Read `frames` → 你写 Markdown。有 `comments_danmaku` 加「观众观点」一节（不捏造）。这是可选分支，不是默认。
4. **handoff**：只有返回 `handoff: true`（或 generic **下载失败**）才接手。未知 URL 现在是 `generic`（yt-dlp），**不要**一看到非内置平台就当失败。接手：WebFetch / 浏览器取源，或下到本地再 `generate_note(platform="local")`。
5. **API key / Cookie / HF token 绝不进对话或 MCP 参数。**

## 工作流（默认 = 配置 LLM）

```
health_check → (必要时 inspect_video / health_check(url)) → generate_note
    → task 直到终态 → 呈现 markdown
```

- 视频理解 / 弹幕评论 / 截图：用户提起或 setup 已开才传参。
- 多集（合集/分P/播放列表）：一条 `batch_generate_notes`；互相独立的链接才各自 subagent。
- 输出格式、合并音频、说话人分离：用户要时再做（见 `reference/tools.md` / `output-formats.md`）。

## 配置

- 首次：`/videonote-setup`。默认值来自插件 userConfig 或 `! videonote setup`。
- Skill 和 MCP 对不上：`health_check.skill_refresh` 里的 disable + install。

## 参考

用 **Read** 打开：
- [`reference/tools.md`](reference/tools.md) —— 工具参数（含手动模式要问的项）
- [`reference/troubleshooting.md`](reference/troubleshooting.md)
- [`reference/output-formats.md`](reference/output-formats.md)
