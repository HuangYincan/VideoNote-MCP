# zju-lab —— Typst 实验报告 / 理工科笔记 / 论文模板

一份用于 **理工科笔记、实验报告、论文** 的 Typst 模板，含封面、目录、页眉页脚、标题、代码块、数学公式、表格、图表编号、参考文献等内容。

## 特性

- **封面**：课程 / 报告名 + 学院 / 专业 / 姓名 / 学号 / 日期（带 **ZJU 校徽与校名 logo**）
- **目录**：章节目录 + 可选的图表索引
- **正文**：两级标题编号、公式/图表自动编号、代码高亮（codly）、内容块（gentle-clues / colorbox 等）、定理环境（thmbox）
- **引用**：`works.bib` + `#bibliography` 参考文献
- **水印**：可选斜排文字水印（`watermark: "ZJU"`，不写或留空则无水印）

## 使用

模板文件（`template.typ` / `imports.typ` / `img/`）需与你的 `main.typ` 同目录。在 `main.typ` 顶部：

```typ
#import "imports.typ": *          // 引入模板依赖的若干 @preview 包
#import "template.typ": project, indent

#show: project.with(
  course: "计算机网络",
  lab_name: "TCP/IP实验",
  stu_name: "姓名",
  stu_num: "学号",
  major: "专业",
  department: "学院",
  date: (2026, 8, 4),             // 年、月、日
  show_content_figure: true,      // 目录页是否加图表索引
  watermark: "ZJU",               // 水印文字；不写或留空则无水印
)
```

然后编写正文：`= 一级标题` / `== 二级标题`、`$ ... $` 数学公式、```` ```cpp ```` 代码块，各种组件用法见 `demo.typ`。

**编译**：

```bash
typst compile main.typ main.pdf    # 或 vscode + tinymist 插件实时预览
```

模板依赖的 `@preview/*` 包由 typst 包管理自动拉取（需联网），无需手动安装。

## 适配其它学校

ZJU logo 在 `img/` 目录（`ZJU-logo.png` 校徽、`ZJU-name.png` 校名）。其他学校把这两张图换成自己的即可。

## 来源与许可

本模板改编自 [Starlight0798/typst-zju-lab-template](https://github.com/Starlight0798/typst-zju-lab-template)（MIT License，Copyright (c) 2024 Starlight0798），**保留 ZJU 校徽/校名 logo**。本仓库将其原样引入并随 VideoNote-MCP 分发，未作功能修改；MIT 许可文本见同目录 `LICENSE`。原作者的版权与许可声明予以保留。