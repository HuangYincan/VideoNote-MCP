# LaTeX 模板来源与许可（NOTICE）

`templates/latex/` 下的两个模板目录改编自第三方仓库：

- **Math Note** —— 数学 / 理工科笔记风（`\documentclass{MathNote}`，中文版 `MathNoteCN`）
- **English Article** —— 英文文稿 / 演讲大纲风（`\documentclass{article}`）

## 来源

- 上游仓库：[https://github.com/Gua927/Latex_Template](https://github.com/Gua927/Latex_Template)
- 上游目录：`Math Note/`、`English Article/`
- 上游许可：**LPPL-1.3c**（LaTeX Project Public License v1.3c）

## 修改内容

相比上游，两个模板均做了以下修改：

- **删除中国人民大学统计学院校徽（`logo-RUC.png`）的整页背景水印**。具体移除 `\usepackage{background}` + `\backgroundsetup{...}` 水印代码，并删除 `logo-RUC.png` 图片。
- 其余内容与上游一致；README 按本仓库用途改写。

## 许可与义务

- 这两个模板及其派生内容按 **LPPL-1.3c** 分发（参见同目录下 `LICENSE-LPPL-1.3c.txt`）。
- 它们是**修改版**，由 VideoNote-MCP 维护，与上游作者无关；如有问题请向 VideoNote-MCP 反馈，不要联系原作者。
- 完整未修改原版可从上游仓库获取：https://github.com/Gua927/Latex_Template
- 原版权归属原作者（Gua927 / 中国人民大学统计学院）。本仓库对其余代码（非模板）采用 MIT 许可，LPPL 不传染到 MIT 部分。

## 变更日志

- 2026-08-04：引入两个模板，删除 RUC 校徽背景水印，改造 README。