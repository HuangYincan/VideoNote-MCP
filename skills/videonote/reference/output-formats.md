# 输出格式参考 —— 从 MD 底稿到任意格式

> 本文件是 SKILL 的参考（非核心）。**分工**：确定性机械格式（SRT/VTT/JSON）由 MCP 工具
> `process_media(action="export")` 直接产出；创意/自定义格式（思维导图/闪卡/LaTeX/typst/用户模板）
> 由 **Agent 把 MD 底稿当信息源自行生成**。这样 MCP 保持精简，输出格式无限扩展。

## 信息源：拿到底稿

任务成功后（`task(task_id)` 返回 `SUCCESS`）：

1. **`cleanup(task_id, dry_run=True)`** —— 列出任务产物，找到 `gen/note.md`（MD 底稿）与 `gen/transcript.json`（转写）。
2. **`result.markdown`**（MD 底稿）—— 在 `task(action="status")` 的轻量结果里直接有，或读 `gen/note.md`；需要转写时用 **`task(task_id, action="transcript")`**（`full_text` 全文 + `segments` 时间轴，可按段切片）。
3. 便携笔记：`result.note_dir` 指向 `note.md` 所在目录（默认 `{task_id}/gen/`）；若生成时指定了 `notes_dir`，额外有 `result.portable_note_dir` 指向便携副本目录（`<notes_dir>/<标题>/`）。

底稿 = **转换的信息源**。所有格式转换都以它为依据，不再重新下载/转写。

## 机械格式（调 MCP 工具，不自己写）

`process_media(action="export", task_id=..., formats=["srt","vtt","json"], out_dir?)`

- 确定性渲染（时间轴换算），**不耗 LLM、不耗 token**，结果可离线核对。
- 返回 `{task_id, formats: {fmt: "file://绝对路径"}}`，直接 Read 即可用。
- 适用：字幕文件（SRT/VTT）、结构化转写（JSON）、给下游程序消费。
- 任务成功后若 setup 配置了「导出格式默认」，会自动导出这些格式——Agent 可直接读产物文件。

## 创意格式（Agent 基于底稿生成）

原则：读 `result.markdown` 底稿（`task(action="status")` 轻量结果 / `gen/note.md`）→ 按目标格式重写为对应文件 → 交付路径。

### 思维导图（Mermaid）
1. 读 MD 底稿，提炼层级大纲（标题 → 子要点 → 细节）。
2. 写 `mindmap.mmd`（Mermaid `mindmap` 语法），顶层为视频主题。
3. 可选：先给用户看大纲确认，再落文件。

### 闪卡（Anki/Q&A）
1. 从底稿抽取知识点 → 每张卡「问题 / 答案」。
2. 写 `flashcards.md`（`Q: …\nA: …` 格式），便于导入 Anki。

### LaTeX（模板驱动）
1. **列出 `templates/latex/` 下的模板让用户选**（默认 `Math Note`）：
   - `Math Note/` —— 数学/理工科笔记风（`\documentclass{MathNote}`；中文换 `MathNoteCN`；含定理/引理/定义/推论/例题/命题/证明/注记环境）
   - `English Article/` —— 英文文稿/演讲大纲风（`\documentclass{article}` + 摘要/章节/多级列表/参考文献）
2. **Read 所选模板子目录的 `main.tex` 及同目录 README**（新模板无 frontmatter；README 说明文档类、中英文切换与编译注意）。
3. **以模板为骨架、底稿为信息源**，生成 `note.tex`：Math Note 用 definition/theorem/lemma/proof 等环境组织数学内容，English Article 用 section/abstract/thebibliography 组织英文稿。
4. 可选编译 PDF：若系统有 `xelatex`（`which xelatex`），把模板的 `.cls`（Math Note 需 `MathNote.cls`/`MathNoteCN.cls`）随 `note.tex` 一起放到位再编译；Math Note 背景图需**连续编译两遍**；没有 xelatex 则只交付 `.tex`。
5. **用户自定义模板**：用户提供 `.tex` 路径或放到 `templates/latex/`，同样处理。

### typst / 其他自定义模板
1. **内置模板**：`templates/typst/zju-lab/` —— 理工科笔记 / 实验报告 / 论文风（封面 + 目录 + 页眉页脚 + 公式/图表编号 + 代码块 + 参考文献，带 ZJU 校徽）。
2. **使用**：把 `template.typ`、`imports.typ`、`img/` 与生成的 `note.typ` 放同目录，在 `note.typ` 顶部 `#import "template.typ": project` + `#show: project.with(course: ..., lab_name: ..., ...)`；正文按 `demo.typ` 示例组织（`= 标题`、`$公式$`、代码块、`#bibliography`）。
3. **编译**：若系统有 `typst`（`which typst`），`typst compile note.typ note.pdf`（依赖的 `@preview/*` 包自动拉取）；没有则只交付 `.typ`。
4. **用户自定义模板**：用户提供 `.typ` / 其他格式模板文件，同样处理（读模板结构 → 把底稿内容填入 → 输出结果文件）。

## 输出落盘位置

- 机械格式：`process_media(action="export")` 写到 `note_results/{task_id}/`（`out_dir` 可覆盖），并记入 manifest（可被 `cleanup` 清理）。
- Agent 手写格式：写到 `note_dir`（若有）或当前工作目录，交付路径给用户。
