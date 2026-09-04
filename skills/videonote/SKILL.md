---
name: videonote
description: 用 VideoNote-Mcp 的 MCP 工具把视频链接/本地视频（B站/YouTube/抖音/快手/小宇宙/小红书）生成 AI Markdown 笔记。触发词：「生成视频笔记」「视频 → 笔记」「帮我给这个视频做笔记」「从 XX 链接做笔记」。
---

# VideoNote-Mcp —— 视频 → AI 笔记

笔记默认由**当前对话里的你**写。配置 LLM（`generate_note` / `batch_generate_notes`）是后备。

**判定（自己做，不要问用户）**：
- 你能阅读图片（对本对话中的图片 / `file://` 帧做 Read 能看到画面）→ **默认路径**。
- 你是纯文本模型、Read 图片只能得到路径或报错、或宿主无视觉 → **后备 LLM**。
- 用户明确说「用配置的 LLM / generate_note / 全自动 LLM / 不要你自己写」→ **后备 LLM**（即使你能看图）。

不要先问全自动还是手动，不要列 6 项等确认。`get_config` 里的 `agent_direct` **server 不读**；旧向导默认关，**不要**因为该键是 false 就改走 `generate_note`。

## 默认路径（你来写）

1. 没有 `mcp__videonote__*` → 停，让用户重启会话 / `/reload-plugins`。不要用 CLI 代替 MCP。
2. `health_check(need_provider=False)`：缺 ffmpeg 先让用户装。**不要**因为 `keyed_providers=0` 去要 API key。
3. `inspect_video(url)`（识别平台 + 检查链接 + 拆多集；链接无效会直接给原因）。
4. `kind=single`：一条 `prepare_note_material`。你能看图时传 `video_understanding=True`（抽帧给你 Read）。
   `kind=multi`：每条 `entries[].url` 各自 `prepare_note_material`（用户没说「只要第 N 集」就处理全部；太多先报 `total`）。
   **互相独立的多个链接**：每个 url 一个 **subagent**（提交 → 轮询 → 读素材 → 写笔记 → 汇报），主 agent 汇总。
   **合集 / 分 P / 播放列表**：同一会话逐条提交（admission 默认 3，超限会拒；先提交最多 3 个，完成再下一批）。不要同一消息并行多个 `prepare_note_material`。**不要**对默认路径用 `batch_generate_notes`。
5. 长视频 / 首次使用 / 最近改过转写引擎 → `health_check(need_provider=False, url=url)`。
6. **`task(task_id)` 轮询**到 SUCCESS / FAILED / CANCELLED。取消是协作式：可控下载/ffmpeg 会尽快终止；B 站弹幕/评论会在下一请求边界停止，正在进行的 HTTP 仍要等返回，不要向用户承诺立即硬停。
7. `task(task_id, action="transcript")`（默认前 50 段；全文 `segment_range="all"`）+ Read `frames` → **你写 Markdown**。有 `comments_danmaku` 加「观众观点」一节（不捏造）。
8. 呈现笔记。用户要细节再用 transcript。**不要主动问「要不要后续优化」**。用户要精修再读笔记 + 转写改。

用户改某一项（风格 / 视频理解 / 评论 / 截图）→ 只改那一项再跑。用户说「手动」才逐项问（见 reference）。

## 后备路径（配置 LLM）

仅当你**不能看图**，或用户明确要求配置 LLM：

1. `health_check(need_provider=True)`：`keyed_providers=0` → 让用户 `! videonote providers set`（key 不进对话）。
2. 单集：`generate_note(video_url)`（`provider_id` 可省略，套 setup 默认）。合集 / 分 P / 播放列表：一条 `batch_generate_notes`（单次最多 50 条，服务端排队，**不要并发多个 batch**）。
3. **`task(task_id)` 轮询**到终态，呈现 `result.markdown`。
4. **转写素材自动优先平台官方字幕**（YouTube/B 站人工+自动字幕 / 小宇宙官方文稿）；无字幕才走本地转写。小宇宙官方文稿需先 `! videonote login xiaoyuzhou`。

## 强制规则

1. **必须用 MCP 工具**。凭证例外：本会话 `! videonote providers set`、`! videonote login bilibili`、`! videonote login xiaoyuzhou`、`! videonote login xiaohongshu`。
2. **单视频一回合一个提交**。不要在同一条消息里并行多个 `generate_note` / `prepare_note_material`（客户端不稳）。普通任务在提交锁内预占名额，覆盖排队和执行全生命周期，超限拒绝。`batch_generate_notes` 只用于后备 LLM 的合集/播放列表，绕过普通 admission，由线程池排队。
3. **默认你自己写笔记**（见上）。后备才走配置 LLM。
4. **handoff**：只有返回 `handoff: true`（或 generic **下载失败**）才接手。未知 URL 现在是 `generic`（yt-dlp），**不要**一看到非内置平台就当失败。接手：WebFetch / 浏览器取源，或下到本地再 `prepare_note_material` / `generate_note(platform="local")`。
5. **API key / Cookie / HF token 绝不进对话或 MCP 参数。**
6. **`process_media` 保持同步**：export / merge / diarize 不登记后台任务注册表，没有可供 `task(action="cancel")` 控制的任务；需要取消时不要承诺能硬中断其第三方阻塞调用。

## 工作流

```
默认：health_check(need_provider=False) → inspect_video → prepare_note_material
      → task 直到终态 → 读转写/帧 → 你写 Markdown
后备：health_check(need_provider=True) → generate_note | batch_generate_notes
      → task 直到终态 → 呈现 result.markdown
```

- 视频理解 / 弹幕评论 / 截图：默认路径你能看图就开抽帧；其余用户提起或 setup 已开才传参。
- 输出格式、合并音频、说话人分离：用户要时再做（见 `reference/tools.md` / `output-formats.md`）。

## 配置

- 首次：`/videonote-setup`。默认路径不需要配置 LLM key；后备路径才需要。
- Skill 和 MCP 对不上：`health_check.skill_refresh` 里的 disable + install。

## 参考

用 **Read** 打开：
- [`reference/tools.md`](reference/tools.md) —— 工具参数（含手动模式要问的项）
- [`reference/troubleshooting.md`](reference/troubleshooting.md)
- [`reference/output-formats.md`](reference/output-formats.md)
