# VideoNote-MCP 使用案例：一条 Prompt，三个视频自动生成精修笔记

> 历史案例：以下记录保留当时的 Skill 工作流。当前版本不再分发 Skills；MCP 可独立运行，安装说明以仓库 README 为准。

## 前置参数

- LLM 多模态模型已配好（Gemini API，实际使用 gemini-3.1-flash-lite）
- 语音转录模型：Bcut（云端，无需本地下载模型）
- B 站账号已扫码登录（SESSDATA）
- Agent 环境：配有 DeepSeek-V4-Flash 的 ClaudeCode 环境
- 基于 v0.1.1 的 VideoNote-MCP 生成

## Prompt

用户只给了下面这几句话（3 个链接 + 1 个输出目录，**一个参数都没说明**）：

> 利用 VideoNote，在 `Path/To/Note` 中，生成以下三个视频的笔记：
>
> - [雅思教父刘洪波的雅思真经第一课_哔哩哔哩_bilibili](https://www.bilibili.com/video/BV1c54y187SH/)
> - [和解剖了4000具尸体的法医，一起看了影片中的尸体..._哔哩哔哩_bilibili](https://www.bilibili.com/video/BV1QEgZ6rEGj/)
> - [李宏毅 | 自注意力机制和Transformer详细解析_哔哩哔哩_bilibili](https://www.bilibili.com/video/BV1r8nMz4EAj/)

## 亮点

Agent 依据 VideoNote 的 SKILL 全自动跑完：**环境检查 → 链接识别 → 找到唯一已配 key 的供应商 → 列模型 → 逐项询问参数 → 多视频并行生成 → 主动询问并执行后续优化**。用户全程无需了解任何工具细节，只需回答几个选择题。

## 模型回答过程

### 1. 环境检查与链接识别

- `health_check`：FFmpeg ✅ / 数据库 ✅ / 转写引擎 bcut（云端）就绪 ✅
- `validate_url` ×3：三个链接均识别为 B 站视频

### 2. 供应商与模型发现

- `list_providers`：已配置 key 的供应商只有一个 —— **Gemini**
- `list_models`：Gemini 2.5 / 3.x 系列模型可选

### 3. 参数确认（SKILL 强制：未确认参数前不调用 generate_note）

Agent 一次性提问，用户逐项选择：

| 参数 | 用户选择 |
|---|---|
| LLM 模型 | **gemini-3.1-flash-lite** |
| 笔记风格 | **detailed**（详细，推荐） |
| 视频理解（逐帧截图分析画面内容） | **开启，6s 间隔**（Gemini 多模态支持） |
| 整合 B 站弹幕 + 评论区观点 | **20 条** |
| 笔记中插入截图 | **开启** |
| 输出目录 | 用户指定（Prompt 里的 `Path/To/Note`） |

### 4. 多视频并行生成

三个视频按 SKILL「多视频用 subagent 并行」规则，**各起一个 subagent**，独立完成 `generate_note` → 轮询 → 汇报；主 agent 汇总。
统一配置：`gemini-3.1-flash-lite` · detailed · 6s 视频理解 · 弹幕评论 20 条 · 插截图。

### 5. 生成结果

3 份笔记全部落盘：`note.md`（结构化 Markdown）+ `Assets/`（截图）+ 「观众观点」章节。

### 6. 后续优化（生成后 Agent 主动询问）

- Agent 问「需要后续优化吗（基于笔记 + 完整字幕精修）？」→ 用户确认
- 按任务 ID 定位三份**完整转写字幕**（全文 + 分段）
- 3 个精修 subagent 并行：读「字幕 + 原笔记」→ 挖更多细节、补齐遗漏、修正不一致、增强结构 → **保留 `note_original.md` 供对比、覆盖 `note.md`**
- 校验截图引用与 Assets 实际文件**完全一致，0 断链**

## 输出结果

三份笔记，每份均为 **精修版 note.md** + **原版 note_original.md**（对比用）+ **Assets/ 截图** + **「观众观点」章节**。

### 1. 雅思真经第一课（BV1c54y187SH）

📄 [精修版 note.md](notes/ielts-true-class/note.md)（24KB / 270 行 / 18 张截图）· [原版](notes/ielts-true-class/note_original.md)

- **破误区**：刷题 = 模考测水平，不提升基础能力只会让现有分数更稳定；单纯背单词 ≠ 正确率提升
- **听力**：四 Section 场景拆解 + 精听跟读 4 步法 + 179 个高频考点词（同义替换）
- **阅读**：538 核心考点词、`resemble` 一题三考点逐层拆解、逻辑记忆法
- **写作**：TR/CC/LR/GRA 评分框架、15 句逻辑框架（不用填空式模板）
- **口语**：88 个功能句型 > 话题素材；备考四步路线 + 八本教材清单
- 「观众观点」：含对 "environment" 发音的争议

### 2. 和解剖了 4000 具尸体的法医（BV1QEgZ6rEGj）

📄 [精修版 note.md](notes/forensic-doctor-reacts/note.md)（12KB / 182 行 / 9 张截图）· [原版](notes/forensic-doctor-reacts/note_original.md)

- 从业 43 年、解剖 4000+ 例的刘良法医「拉片」对比影视与现实
- 尸臭的记忆性、开颅电锯「吃硬不吃软」、骨盆辨性别
- 「诈尸」真相 = 超生反应；家暴淤青识别 + 必须立刻取证报案
- 真实法医工具箱（汤勺量积血、凿子 + 扩张器开颅）
- 精修后结构扩为 **12 节**（视频速览 → 观众观点），补器官「装回去」、吃人肉案例、求生意志等遗漏板块
- 「观众观点」：法医「为死者开口说话」

### 3. 李宏毅｜自注意力机制和 Transformer（BV1r8nMz4EAj）

📄 [精修版 note.md](notes/transformer-self-attention/note.md)（20KB / 199 行 / 18 张截图）· [原版](notes/transformer-self-attention/note_original.md)

- 为什么需要 Self-attention：输入是一排向量、长度不固定
- 输出三种类型 / Sequence Labeling 的挑战 / Self-attention 机制
- 精修补全：Attention Is All You Need 背景、Self-attention 计算 Step 1–5（点积 / 加性注意力）、"I saw a saw" 完整讲解
- **18 张截图按讲课时间线分布到对应小节**

## 演示了什么

一次极简 Prompt（3 链接 + 输出目录）→ 自动完成：**参数确认 → 多视频并行 → 视频理解（截图）→ 弹幕/评论整合 → 生成后基于字幕精修**，产出带截图、含「观众观点」的便携 Markdown 笔记。
