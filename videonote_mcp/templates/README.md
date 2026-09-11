# 独立导出模板与多格式输出

模板是随 VideoNote-MCP 安装包分发的资源，不是 Skills，不需要额外安装或访问 GitHub。
本目录从 Git 历史 `faf7894^:skills/videonote/templates/` 恢复；保留原许可、来源声明、源码、图片与样例 PDF。
原始模板内容仅作排版示例，不能当作用户笔记的事实来源；样例 PDF 也不是此次导出的结果。

## 获取底稿

- 已有 Markdown 笔记直接复用；否则读取成功任务的转写：`task(action="transcript", task_id=..., segment_range="all")`，必要时结合帧图写底稿。
- `task(action="status")` 可取得任务结果；素材任务不保证已有 `note.md`，应以实际产物为准。
- 不为换输出格式重新下载或转写视频。

## 字幕 / 结构化转写

`process_media(action="export", task_id=..., formats=["srt", "vtt", "json"])`

这是确定性导出器，默认写入任务的 `gen/`，支持 `out_dir`。不接受 latex/typst/pdf 等格式。

## 列出、读取、复制模板

1. `process_media(action="template")` 列出模板和本机编译器是否存在。
2. `process_media(action="template", template_id="latex-math-note")` 列该模板的文件与 URI。
3. `process_media(action="template", template_id="latex-math-note", template_file="main.tex")` 读取源码；同理读取 README.md、文档类、Typst 文件等。
4. `process_media(action="template", template_id="latex-math-note", out_dir="<data_dir>/exports/my-note")` 复制完整配套文件到**新的目录**。已有目录（包括空目录）一律拒绝覆盖；复制失败时可能留下部分文件，应核查后换新目录重试。LaTeX 副本自动带上 NOTICE.md 与 LPPL 许可证。
5. 在副本中新建 `note.tex` / `note.typ`，将模板样例内容替换为底稿，不修改安装包中的原件。

MCP Resources：`videonote://templates` 列目录，`videonote://help/export` 读本指南，`videonote://templates/{template_id}` 列配套文件。
客户端不支持 Resources 时，以上工具路径完全可用；本指南也可用 `process_media(action="template", template_file="GUIDE.md")` 读取。
复制输出默认限于 `health_check.data_dir` 内；用户显式配置 `VIDEONOTE_ALLOW_EXTERNAL_PATHS=1` 后才可写其它目录。
模板副本不登记任务 manifest，`cleanup` 不负责回收数据目录 `exports/` 下的独立副本。
MCP 不执行模板、安装编译器或下载编译依赖；不要求 Agent 具有 Shell 才能列举和阅读模板。

| ID | 模板 | 入口 | 许可 |
|---|---|---|---|
| `latex-math-note` | Math Note，中英文数学/理工笔记 | `main.tex`、`MathNote.cls` / `MathNoteCN.cls` | LPPL-1.3c，见 latex/NOTICE.md |
| `latex-english-article` | English Article，英文文稿 | `main.tex` | LPPL-1.3c，见 latex/NOTICE.md |
| `typst-zju-lab` | zju-lab，理工科笔记/实验报告 | `example.typ`、`template.typ`、`imports.typ`、`img/` | MIT，见模板目录 LICENSE |

## LaTeX → PDF（可选，由用户授权的终端工具执行）

先读所选模板 README.md 和源码。Math Note 默认英文 `\documentclass{MathNote}`，中文用 `MathNoteCN`；文档类和图片须与生成的源码保持相对路径。

在导出副本目录内执行两遍（目录中有空格时必须正确引用路径）：

```bash
xelatex -no-shell-escape -interaction=nonstopmode -halt-on-error note.tex
xelatex -no-shell-escape -interaction=nonstopmode -halt-on-error note.tex
```

需本机安装 XeLaTeX、相应宏包和字体。原 MathNoteCN 写死 `KaiTi`；macOS/Linux 缺该字体时，应先确认可用中文字体，再**在副本**中将字体改为已安装的 `Kaiti SC` / `FandolKai-Regular.otf` 等，不自动安装未知字体。
若 `ctexart` 自动选择的系统字体组也缺字体（如 macOS 的 `STHeiti`），仅替换 KaiTi 仍不够。已安装 TeX Live Fandol 字体时，可在副本 `MathNoteCN.cls` 使用 `\LoadClass[a4paper,fontset=fandol]{ctexart}` 和 `\setCJKmainfont{FandolKai-Regular.otf}`；先确认字体可用，再实际编译。
生成文件须移除示例中的重复标签并确保交叉引用指向自己的内容；英文历史样例存在重复 label 警告，不应原样沿用。
不启用 shell-escape。未安装编译器或编译失败时，交付 `.tex`、`.cls`、必要图片和许可证，明确告知尚未产出 PDF。只有实际编译成功且核对内容后，才能交付生成的 `note.pdf`。

## Typst → PDF（可选）

读 `example.typ`，将 `template.typ`、`imports.typ`、`img/` 与生成的 `note.typ` 保持在同一目录；参考文献如有使用还需 `works.bib`。
`#import "template.typ": project`，用 `#show: project.with(...)` 设置标题等；参数必须基于用户实际信息，不沿用示例身份。
本模板带 ZJU 标识，不适用时在副本调整封面，不把它当用户机构。源码沿用历史版本，字体/依赖兼容性需实际编译验证。

```bash
typst compile note.typ note.pdf
```

`imports.typ` 使用固定版本 `@preview/*` 包；首次编译未缓存依赖时可能需联网。缺少 Typst、字体、依赖或网络时，交付完整 `.typ` 源码及配套文件，说明阻塞原因，不谎称 PDF 已生成。

## 其它格式与自定义模板

- 思维导图：基于底稿生成 Mermaid `mindmap` 文件。
- 闪卡：基于底稿生成 Q/A 文本或按目标导入器要求生成文件。
- 用户模板：使用客户端经用户授权的文件工具读取模板并转换；MCP 模板入口仅访问内置白名单，不接受任意文件路径。

仓库 README / docs/04-使用手册.md 负责安装和配置说明；本指南是随包的离线导出说明，不要求加载 Skills。
