# VideoNote-MCP 使用案例：agent_direct 多源整合 + LaTeX mathnote PDF

一条视频 + 四类外部资料(论文 / 技术报告 / 公众号官宣 / 开源集合)→ **AGENT 直接生成**精修笔记,并输出 **LaTeX mathnote 格式 PDF**。

## 前置参数

- 转写引擎:Bcut(云端,无需本地模型)
- **未配置可用 LLM**(内置供应商均无 key)→ 走 **`agent_direct` 分支**:`prepare_note_material` 只取素材,笔记由 Agent 自己写
- 视频理解:开启(逐帧抽图,多模态模型 Read 看图)
- B 站弹幕 + 评论:已抓取
- LaTeX 工具链:`xelatex`(TeX Live 2026);模板用用户自有的 `MathNoteCN.cls`(中文楷体 mathnote 模板)
- 输出落点:Obsidian vault `技术/00. 技术杂文/`

## Prompt 序列

用户**没有一次说全**,而是像对话一样逐步追加需求:

```
1. 检查下你能不能用 videonote
2. 直接走agent生成，带视频理解和截图存放，把这篇视频 <BV1rpovBCEGH>
   撰写笔记至技术/00. 技术杂文，精修笔记后保留原笔记，
   结合论文 <arXiv:2606.19348>、技术报告 <HF PDF>、公众号官宣 <微信链接>、
   开源链接 <HF 集合> <ModelScope 集合>，再选latex的mathnote格式输出为pdf
```

## 亮点

- **`agent_direct` 全流程**:不依赖配置 LLM,Agent 读转写 + 帧图 + 评论,自己写笔记——"视频理解"由 Agent 的多模态能力完成
- **多源交叉整合**:视频转写 × 论文全文 × 技术报告 × 开源模型清单,汇成一篇有评注的学术向技术杂文
- **精修保留原稿**:产出 `note_original.md`(初稿)与 `note.md`(精修版)双份对比
- **LaTeX mathnote PDF**:复用用户自己的 `MathNoteCN.cls` 模板,自适应修复字体缺失(KaiTi→STKaiti)、断行溢出、参考文献去重

## 模型回答过程

### 1. 能力检查
- `health_check`:FFmpeg ✅ / 数据库 ✅ / 转写引擎 bcut ✅
- `list_providers`:内置供应商均无 key,仅一个 custom 供应商有 key → 可选,但用户指定走 agent_direct

### 2. 素材准备(agent_direct)
- `validate_url` → 识别为 bilibili
- `prepare_note_material(video_url, video_understanding=True, video_interval=6, grid_size=[3,3], include_comments=True)` → `task_id`
- 轮询 `get_task_status` → `SUCCESS`,拿到:331 段转写(≈4.4k 字)、13 张帧图、弹幕汇总 + 20 条热评
  > 注：历史案例实录，工具名为当时所用；#138 工具精简 16→10 后任务轮询入口为 `task`（get_task_status / get_task_transcript / cancel_note 合并）。

### 3. 外部资料抓取
- `WebFetch` arXiv 摘要 → 拿到 V4-Pro/Flash 规格与效率数据
- 下载 arXiv PDF → `markitdown` 转文本 → 提取架构/训练/基建/评测细节
- HF 集合页 → 7 个开源模型清单;ModelScope 页(JS 渲染)、公众号官宣(微信验证墙)→ 改用搜狗微信搜索确认信息,链接保留在笔记中

### 4. 写笔记 + LaTeX PDF
- 写初稿 → 精修(补阅读地图/技术评注/数学直觉),保留初稿
- 发现 `MathNoteCN.cls` 写死 `KaiTi`(macOS 无此字体)→ 就地改用 `STKaiti` 编译 `xelatex`

### 5. 输出
三份产物落盘 Obsidian:精修版 md、初稿 md、mathnote PDF(11 页)。

## 输出结果

📄 [精修版 note.md](notes/deepseek-v4-agent-direct/note.md)(304 行 / 20KB · 含阅读地图 + 技术评注)· [原版](notes/deepseek-v4-agent-direct/note_original.md)(153 行 / 10KB)
📄 [LaTeX mathnote PDF](notes/deepseek-v4-agent-direct/DeepSeek-V4%20技术解读（精修版）.pdf)(11 页 · 学术引用)
📄 [LaTeX 源文件 + 模板](notes/deepseek-v4-agent-direct/latex/)(含 `MathNoteCN.cls`、`logo-ZJU.png`、帧图,`xelatex` 可直接重编译)

笔记要点:

- **一句话**:V4 不是"更大",而是用 **CSA+HCA 混合注意力 + mHC 残差约束 + Muon 优化器**把百万 token 上下文变成可常规部署的能力(1M 下 FLOPs 只需 V3.2 的 27%、KV cache 10%)
- **技术演进主线**(视频):V1 缩放定律 → V2 DeepSeekMoE+MLA → V3 671B/37B → R1 纯 RL → V3.2 DSA → V4 CSA+HCA
- **架构**:Lightning Indexer、mHC 双随机矩阵(Sinkhorn 投影)、Muon 配置
- **训练与基建**:数据构建、结构参数表、Anticipatory Routing、OPD 全词表蒸馏、细粒度 EP/TileLang/确定性 kernel/磁盘 KV cache/FP4 QAT
- **评测**:Pro-Max 落后前沿 3–6 个月;SimpleQA/FACTS 相对 V3.2 近乎翻倍
- **观众观点**:从弹幕/热评提炼(「满本都写着'没卡'」「v4 缓存命中一折」)

## 演示了什么

一个**零参数说明**的 prompt + 后续对话式追加需求 → Agent 自动完成 **素材准备 → 多源资料抓取 → 自写笔记 → 精修保原稿 → LaTeX 模板适配 → 学术引用规范化 → 按用户反馈迭代**,最终交付带水印、可重编译的 mathnote PDF。相比自动 LLM 生成案例,本案例展示了 agent_direct 的"Agent 即写手"模式与高定制化输出(模板/水印/引用/排版)的完整链路。
