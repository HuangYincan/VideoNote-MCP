# CHANGELOG

按关键节点记录项目变更（日期 + 做了什么 + 文档改了什么）。

## Wave D 批 1-5（2026-08-17 · 52 项全部落地，198 tests）

- **批 1 安全（c5c65d2）**：print 全清（抖音/快手/helper/groq/video_helper——含 Cookie 打印）；tiktok 平台映射→generic；requests 全带 timeout；下载器惰性工厂 `get_downloader()` 每次新建实例 + atexit 兜底清理 cookie 文件（模块级单例 `__del__` 永不触发）；bilibili SRT 解析 CRLF 归一化。
- **批 2 发布（6c3ffa1）**：Dockerfile 删失效 `COPY events/`；pyproject 去掉 `events/**`；`fastmcp>=3,<4` pin；release.yml 加 test gate（ruff + pytest）、三处版本核对、`uv build --no-sources` + wheel 内容验证。
- **批 3 状态机（5a9755b）**：default_model 分支检查 key；cancel_note 竞态（`_status_is_terminal` 已终态不覆盖 SUCCESS）；result.json 原子写（tmp+replace）；`_parse_segment_range` 抛 ValueError→UNKNOWN；preflight 队列满提示；`_write_app_config` 加锁原子写。
- **批 4 可靠性（9ed1b47）**：ffmpeg 全带 timeout=120/600 + 失败反馈（generate_screenshot unlink 半成品）；note_cache 媒体 100MB 上限；缓存键 Windows 安全（`_fs_safe` 冒号→连字符）；时间戳正则 `\d+`；local cover 默认数据目录、douyin stream 分块下载 + aweme_id 校验、generic 下载后 exists 校验；ytdlp_retry 补 2 处（inspect/url 探测）。
- **批 5 分发/门禁/卫生（aaee3a8）**：install.sh 回退修复（marketplace 成功清用户级 mcp add、插件已装跳过 add、步骤 1/3）；plugin.json userConfig 补满 12 个 env；conftest 按 pid 隔离测试目录；CI Python 3.11/3.12/3.13 矩阵 + ruff/pytest 进 `[dependency-groups] dev`（版本随 lock 固定）+ tools/list 断言工具数；CLI export task_id 正则校验、登录失败只打印域名（不泄 crossDomain URL）；server 细节（_MAX_WORKERS 容错、is_file、model_size 校验、extract_frames 收敛 `_submit_step_task`、参数下限钳制）；universal_gpt content None 防护；openai_client http_client weakref.finalize 释放；transcriber rebuild 防御性 close 旧实例。

## 大型改动 6a-6i（2026-08-17 · 9 项全部落地）

- **6a 引擎与默认模型（8b78f65）**：whisper 默认 tiny→small；HF_HUB_DOWNLOAD_TIMEOUT 10→60；health_check 模型行区分 downloaded/downloading/failed(+error)/missing；新增 engine_advice（fast-whisper 配 tiny/base 时建议升级）。#39 语言自动选引擎评估后不做自动切换（需先跑推理才知语言、funasr 非默认安装），改显式建议。
- **6b 视频理解成本（3147ed3）**：`effective_frame_interval` 帧组封顶（≤24 组 3×3 网格，超限自适应拉大间隔不截尾）；截图/视频理解模式视频已下载后从本地提取音频，免第二次网络下载。
- **6c token 级切块（5e620d7）**：RequestChunker 双约束（字节 + token，汉字≈1 token 保守估计）；`OPENAI_MAX_TOKENS_PER_CHUNK` 默认 12000；merge 分组同步；source_signature 含切块策略（旧 checkpoint 不复用）。
- **6d 说话人分离进主路径（5ade3ed）**：`pipeline.apply_diarization` 转写完成后自动打 speaker（预处理/非预处理两路径，generate_note 与 transcribe_media/prepare_note_material 全部生效）；失败只 warning 不阻断；prompt 在 2+ 说话人时渲染 `[SPEAKER_00]` 前缀（会议纪要风格真正吃到 speaker）。
- **6e 播放列表批量（35f900e）**：新工具 `batch_generate_notes`（服务端 inspect 展开 + 逐个排队、max_entries 默认 10、单条失败收集继续）；工具数 37→38。
- **6f 工具面收敛（fa2c89c）**：MCP Resource `videonote://task/{task_id}/transcript`（时间轴纯文本含 speaker）；`_load_task_transcript` 公共函数。
- **6g 敏感信息加密（ce51514）**：新 `videonote_mcp/crypto.py`（Fernet 机器级密钥 `config/fernet.key` 0600、`enc:` 前缀、明文兼容迁移、key 丢失回退 None 不抛）；providers.api_key 与 app_config.hf_token 落盘加密；Claude builtin base_url `https://`→`https://api.anthropic.com/v1/`；`providers add --api-key` 改可选 + getpass 交互；export/merge 数据目录外输出 warning；VENDOR.md 标注 abogus.py GPL-3.0 来源。依赖新增 `cryptography>=42`。
- **6h vendor 边界（864fe9c）**：docs/02 同步纪律（冻结清单引用、惰性 import 约定）；docs/04 修正「所有方式共用同一数据目录」错误说法。
- **6i 插件更新路径（5745a84）**：plugin.json 加 version（版本维护点 3 处）；health_check 返回 plugin_version + 落后时 skill_refresh 点名提示；release 四处版本核对（pyproject/__init__/plugin.json/tag）。

## Wave E（2026-08-17 · E2 + E1 落地，216 tests）

- **E2 大纲传递（af4a198）**：多 chunk 笔记标题漂移根治——新增 `extract_outline`（从已生成 partials 提取 `#`~`####` 标题、清理 markdown 记号与 `*Content-[mm:ss]` 后缀、去重、上限 15 条/40 字）；summarize 循环给每个后续 chunk 的 prompt 注入「已生成的章节大纲」块（含 checkpoint 恢复的 partials，首个 chunk 不注入）；`generate_base_prompt` 新增 outline 参数；MERGE_PROMPT 加强（同义章节合并保留最早标题、分散内容按时间归并）。切块估算不含大纲（留 ~5% 余量）。新测试 tests/test_outline.py 11 项。
- **E1 可观测性收口（1ced97a）**：stderr 日志超限轮转（`VIDEONOTE_STDERR_LOG_MAX_MB` 默认 50MB → `.log.1`，防长跑体积失控）；`_open_stderr_log` 打开失败不再静默（原因打到原始 stderr）；atexit 退出摘要记录进行中/排队任务数（排查孤儿 ffmpeg/whisper 子进程）。新测试 tests/test_stderr_log.py 7 项。
- 文档：docs/06 新增 Wave E 章节；docs/05 #39/#44 标注更新。

## Wave E 批 4（2026-08-17 · 自主改进轮 #90-#109，376 tests）

- **下载器健壮性（f5bdc8b，#90）**：#36 剩余 4 子项——kuaishou 失败点抛明确 RuntimeError（原 TypeError/AttributeError/IndexError 三连）；Bcut 轮询指数退避 `min(1<<i, 5)` + 5 处 HTTP 补 timeout；generic cookie 从「写死 example.com 的 Netscape 文件（永不生效）」改 `http_headers` 直接注入；audio.json 实体悬空视为缓存失效重新下载。tests/test_downloader_robustness.py 16 项。
- **DB 路径修复（5ba8497，#91）**：`app/db/engine.py` 默认 `sqlite:///video_note.db` 是相对 CWD 路径——裸脚本/单文件测试在仓库根分裂出 DB（根目录残留垃圾的真实根因）；改 `get_data_dir()` 稳定路径；`cache_data` 弃用 vendored 旧 `DATA_DIR` env。
- **下载器集成测试（364e7ad，#92）**：真实下载器类 + mock yt-dlp 的全流程 15 项（字段契约/quality 映射/skip_download/多 P glob 缓存/ytdlp_retry 语义）；顺带修 `download_video` 提取失败 `None.mp4` 误导报错（改 ValueError）与 douyin tuple 错误形状。
- **插件 userConfig 对齐（05cb87a + 2c1ad9f，#93/#94）**：全仓 15 个 `VIDEONOTE_*` env 与 plugin.json 对齐（补 max_workers/stderr_log_max_mb，内部路径 3 个刻意不暴露）；`batch_generate_notes` 透传 generate_note 全部高级参数（link/视频理解/弹幕/notes_dir）；`_USER_CONFIG_MAPPED_ENV` 同步（CI 门禁拦截）。
- **死代码清理（#95）**：`app/gpt/` 删 5 个全仓零引用模块（openai_gpt / deepseek_gpt / qwen_gpt / utils / tools——gpt_factory 走 `OpenAICompatibleProvider` 直连，上游直连类死代码）；VENDOR.md 分叉清单同步；AST 全仓死模块普查确认无其余残留。**268 passed + ruff F-clean（Wave C 13 收口）**。
- **CLI 契约测试 + update_provider 假成功修复（#96）**：`ProviderService.update_provider` 对不存在供应商返回恒真 dict——CLI `providers set` 与 MCP `update_provider` 的「不存在」判空永不生效（不存在的 id 被报成「已更新」）；改 `updated_provider is None` 时返回 None。新 tests/test_cli.py 18 项（CLI 此前零测试覆盖：providers 加密落盘 enc:/掩码/交互取 key/错误路径、transcriber 配置闭环、export 渲染 transcript.srt、subprocess seed 验证）；另清 2 处旧 worktree 路径 docstring。**286 passed + ruff F-clean**。
- **style/format 白名单 enum 化（#97）**：4 个工具（generate_note/prepare_note_material/summarize_note/batch_generate_notes）签名 `Literal` 化——schema 呈现 enum（Agent 可见合法值）；`_check_style_and_format` 入口显式校验兜底（FastMCP 不做运行时参数校验，直接调用传非法 style 曾静默降级成「无风格笔记」）；校验在 provider 解析前（H6：不需要 provider 的错误先报）。+7 契约测试。**293 passed + ruff F-clean + 39 工具**。
- **fetch_comments limit 钳制（#98）**：`limit<=0` 令 fetcher 的 `len(seen) >= limit` 恒真——第一页即停止，**静默返回空评论**（ok:true 无数据，Agent 误判视频无评论）；入口钳制 `max(1, int(limit))`。+3 契约测试。**296 passed + ruff F-clean**。
- **notes_dir 数据目录外提示（#99）**：export/merge 的「只提示不拦截」warning（#45 口径）唯一漏网的是 notes_dir——便携笔记写任意绝对路径静默无声；generate_note 解析完缺省链（参数 → app_config → VIDEONOTE_NOTES_DIR）后统一校验，数据目录外打 warning（便携笔记是显式用户意图，不拦截；batch_generate_notes 委托 generate_note 自动继承）。+3 契约测试。**299 passed + ruff F-clean**。
- **grid_size 入口显式校验（#100）**：extract_frames 工具校验 grid_size，但 generate_note / prepare_note_material 直传——非法值（[0,0] / [1] / [1,2,3]）在流水线深处 VideoReader 才炸成「视频处理失败」泛化错误；校验提取为共享助手 `_check_grid_size`（与 style/format 同口径：None/空不拦、入口显式报错），接入 generate_note / prepare_note_material / extract_frames 三处；顺带清 note.py 2 处裸 `except:`。+7 契约测试。**306 passed + ruff F-clean**。
- **num_speakers 无效值显式提示（#101）**：`diarize_audio` 的 `kwargs = {"num_speakers": n} if n else {}`——0 / 负值 / 非 int 静默回退自动检测（用户显式传了无效值却无声无息）；入口校验打 warning 后回退，合法值照常透传。+4 单元测试。**310 passed + ruff F-clean**。
- **list_tasks 分页（#102）**：无上限返回全量（每行含 200 字 summary，长跑用户任务上百条时响应膨胀）；加可选 `limit` / `offset`（缺省全量，向后兼容；limit 钳制 ≥1，offset 钳制 ≥0）；skills tools.md 同步新签名。+4 契约测试。**314 passed + ruff F-clean**。
- **set_transcriber 引擎白名单（#103）**：未知 `transcriber_type` 被持久化后，运行时 `get_transcriber` 的 `TranscriberType(...)` 解析失败**静默回退 fast-whisper**——用户以为配了 groq/bcut 云端引擎，实际在跑本地 whisper；入口白名单校验（与 style/format 同口径；CLI 侧 argparse choices 本就安全），白名单与 TranscriberType 枚举同源防漂移。+3 契约测试。**317 passed + ruff F-clean**。
- **静默错误族收尾（#104）**：① summarize_note 的 transcript 形状——垃圾/空 dict/传 fetch 结果外层 `{"ok": ...}` 曾静默规整成空素材，LLM 拿零转写**凭空生成笔记**（幻觉内容还烧配额）；入口显式报错（provider 解析前，H6；只查字段存在不查内容——静音视频的 `segments: []` 是合法转写不拦）。② extract_frames 的 video_interval 非数值——`int("abc")` 失败静默回退默认 6（用户设了间隔却无声生效默认值）；与 num_speakers 同口径打 warning 后回退，数字字符串照常透传。③ export_transcript 未知格式——底层 exporter 只写 stderr 警告后静默跳过，Agent 请求 `["srt","pdf"]` 拿到 `ok:true` 实际缺 pdf；入口白名单校验（srt/vtt/json，与 exporter FORMATS 同源），config/env 缺省链解析后同样校验。+14 契约测试。**331 passed + ruff F-clean**。
- **误导错误收口（#105）**：① fetch_subtitles 的 platform 拼错（"bilibil"）——`pipeline.fetch_subtitles` 把 `get_downloader` 的 ValueError 吞掉转 None，工具误报「该视频没有可用平台字幕」（其余平台参数工具的平台错误都显式 FAILED，唯独此处静默转换）；入口白名单校验（与 SUPPORT_PLATFORM_MAP 同源防漂移），空串曾因 falsy 静默走自动检测一并显式报错。② merge_audio 不认 `file://`——全工具面唯一漏网（%20 未 unquote、`Path("file:///...")` 判不存在），入口统一 `_coerce_local_path` 规整。③ diarize_media 目录输入——`.exists()` 对目录为 True 穿透到 ffmpeg 深处炸「转换失败」，改 `.is_file()` 与其余本地文件工具同口径。+9 契约测试。**340 passed + ruff F-clean**。
- **app 层静默降级加固（#106）**：4 并行代理对 `app/` 全 8 目录二轮扫描（第一轮 E3 清 4 P0，本轮无 P1）→ 7 项：① 三个配置管理器（cookie/transcriber/proxy）损坏 JSON 静默当空——引擎悄悄回退 fast-whisper、cookie 悄悄消失，最重的是 `cookie_manager.set()` 读损坏文件写回会把**其它平台 cookie 永久抹掉**（数据丢失）；新增 `app/utils/json_store.py`（损坏 → warning + `.corrupt` 备份 + 空配置；`write_json_atomic` tmp+replace+0600）全部接入。② `check_whisper_model_exists` 模型名解析失败谎报「未下载」→ 区分 ValueError + warning。③ `chunk_duration_guess` probe 失败无日志回退 1800s → 分段时间轴漂移数十秒任务照常 SUCCESS，warning 留痕。④ note.py 全局索引同步失败 debug→warning（list_tasks 静默缺任务）。⑤ started_at 读取 `except:pass`→warning。⑥ universal_gpt checkpoint 损坏静默弃用白烧 LLM 配额 → warning。⑦ inspect 播放列表非 http 条目以成功形状返回 → 跳过 + warning，全坏显式错误。VENDOR.md 冻结清单同步。+17 测试（test_json_store.py 10 + test_app_silent_fallbacks.py 7）。**357 passed + ruff F-clean**。
- 文档：docs/05 第三轮 #90-#94、docs/06、CHANGELOG；skills tools.md 同步 batch 新签名；docs/05 Wave B 第 6 条过时 ⏸ 标记修正（Resource 6f 已落地）。
- **输出目录 file:// 规整 + 导出格式垃圾配置（#107）**：① 三处输出目录参数（`generate_note(notes_dir)` / `export_transcript(out_dir)` / `merge_audio(out_dir)`）统一 `_coerce_local_path` 规整——`file:///…` URI 直传曾 `Path("file:///…")` 在 CWD 下建字面 `file:` 垃圾目录，「数据目录外」提示也基于未规整值恒误报；batch 委托 generate_note 自动继承。② `app_config.default_export_formats` 非列表垃圾值（如字符串）令缺省链 `or` 短路——工具入口炸「必须是列表」、自动导出静默失败、env 回退永不生效；抽 `_resolve_default_export_formats()`（非列表 → warning + 回退 env）接入工具与 `_auto_export_transcript`。docstring 与 tools.md 标注输出目录支持 file://。+7 契约测试（OutputDirFileUriTest + DefaultExportFormatsJunkTest）。**364 passed + ruff F-clean**。
- **set_transcriber 尺寸入口校验（#108）**：#103 收口——引擎类型已白名单但 `whisper_model_size` 没有：`set_transcriber("fast-whisper", whisper_model_size="bogus")` 成功返回后配置落盘，任务跑到 TRANSCRIBING 才因模型加载失败（#106 之后 preflight 也只报「未下载」）。改用运行时同源的 `resolve_whisper_model`（whisper_models 注册表：内置档位 / 自定义名 / 含 `/` 的 HF repo_id / 已存在本地目录）入口校验，未知尺寸立即报错；CLI `transcriber set --size`（自由串，download 才有 choices）同口径拒绝。+7 测试（SetTranscriberSizeValidationTest 5 项 + CLI 2 项，含 repo_id/本地目录直通不被误伤）。**371 passed + ruff F-clean**。
- **merge 目录穿透 + batch 单集退化（#109）**：① `merge_audio` 目录输入——merge.py 的 `os.path.exists` 对目录为 True，穿透到 ffmpeg 深处才炸「转换失败」泛化错误（真因是「是目录不是文件」）；入口 `is_file`（与 diarize_media 同口径）返回「文件不存在或不是文件」，file:// 规整（#107）与 is_file 叠加生效。② `batch_generate_notes` 单集退化路径（`kind=="single"`）`_submit` raise 裸传中断调用——多条目循环有「单条失败收集继续」契约、单集没有；收进 errors 返回与多条目同形状（`ok:false, submitted:0`）。+5 契约测试。**376 passed + ruff F-clean**。

## Wave E 批 3（2026-08-17 · 契约收尾 + 截图路径闭环，236 tests）

- **G2 收尾（06f011c）**：video_tasks 表 note 任务列 `note_dir` 改指 `gen/`（与 get_task_status 的 result.note_dir 一致），list_tasks 与 get_task_status 口径统一；NoteDirContractTest 2 项。
- **extract_frames 参数校验（1d5088b）**：interval 钳制 ≥1（非法值回退 6）；grid_size 必须两个正整数；3 项契约测试。
- **G5 退出取消 + YouTube cookie（90a6011）**：_exit_summary set 全部任务 cancel_event（提前终止 ffmpeg/whisper 子进程残留窗口，摘要注明已发送取消）；youtube_downloader 读 setup ③ 填的 youtube cookie → Netscape 临时文件（.youtube.com 域名、0600、__del__ 清理）→ `ydl_opts["cookiefile"]`（download / download_video 两路径）。
- **Assets/ absolutize 修复闭环（781e991）**：H4 修的 `_absolutize_images` 只匹配 `static/screenshots/` 旧全局模式，数据层重构后截图走 `gen/Assets/` + `Assets/xxx.jpg` 相对引用——旧正则不命中，Agent 拿到死路径。加 `base_dir` 参数 + 第二正则（resolve + 目录内校验防穿越），get_task_status 传 `base_dir=result.note_dir`；旧路径保留兼容旧任务；+3 测试。
- 文档：docs/04 get_task_status 补 G3 例外（素材包/transcribe 任务转写是主产物默认返回，仅 note 任务剥）；docs/05 登记 #89；docs/06 Wave E 追加。

## Wave E 批 2（2026-08-17 · E3 扫描 + F1-F9/G1-G4/H 组，225 tests）

- **E3 扫描**：4 并行代理对 Wave D/E 引入代码 + 全仓一致性做第三轮审计，4 个 P0 全部当场修复：
  - **P0 get_task_status 装饰器丢失**：`3c0c9a67` 重构随 `_stage_label` 抽取误删 `@mcp.tool()`，工具 38→37 却随 v0.1.5 发布；CI 数量断言被 batch_generate_notes 补足未红。已恢复，CI 升级精确 39 名单。
  - **P0 图片 token 估算回归**（6c 引入）：base64 图片按字符计数（一张 ≈ 数十万 token）致 video_understanding 静默失效。改结构感知 `_count_tokens`，`image_url.url` 按 1105 token/图固定估算。
  - **P0 merge 永不收敛**：合并轮次复用 `max_tokens_per_chunk`，partials 超限时无限递归。改 merge_chunker `max_tokens=None` + 分组不减少即 raise 明确错误。
  - **P0 wheel 缺 skills/**：release.yml 门禁断言 wheel 含 skills 但 include 没写 → 发布必失败。已加 `"skills/**"` 并重建验证。
- **F4-F9/G1-G4**：CLI key 交互输入（getpass）；provider_dao 解密 None 跳过写入；并发门禁 `not f.done()`→`f.running()`（排队不占名额，batch 语义成立）+ 新增排队放行契约测试；Fernet key O_EXCL 独占创建并发安全；退出摘要写 `sys.__stderr__`（修 atexit 时 logging handler 已关的 I/O error）；conftest 隔离数据目录。
- **G2 note_dir 契约**：`note_dir` 改指 `gen/`（note.md 真实所在），指定 `notes_dir` 时从 manifest 定位便携副本补 `portable_note_dir`。
- **G3 素材包转写保留**：transcribe_media / prepare_note_material 的转写是主产物，`get_task_status` 默认不再剥离（仅 note 任务剥；`raw` 恒剥）。
- **H 组 8 项**：docstring 死代码、`_local_video_exists` 空串/目录误判、started_at 损坏保护、**_absolutize_images 存量 no-op bug**（`m.group(2)` 越界致截图 absolutize 从未生效，新测试暴露）+ 路径穿越防护、stderr env 非法值回退、handoff 前置、install.sh `--no-dev`、`_stage_label` 文档。
- 文档：docs/02/03/00 工具数 39；docs/02/04 Fernet 加密说明 + env 表补 `VIDEONOTE_STDERR_LOG_MAX_MB` + funasr 引擎；docs/04 + skills 平台数口径统一 6；README wait_for_note 废弃标注；skills tools.md/output-formats.md note_dir 与素材包契约；docs/05 第三轮 #77-#85；docs/06 Wave E 更新。

## 第二轮全库扫描（2026-08-17 · 4 个并行审计代理）

- 工具层 / vendored 流水线 / 下载器 / 分发·文档·测试·CI 四路并行扫描，**无新增 P0**。
- 新增 #46-#76 共 31 条发现：P1 7 条（Cookie print 泄漏、tiktok 平台映射错误、requests 无 timeout、下载器 `__del__` 永不触发致 #10 修复未生效、Dockerfile 构建失败、release 无测试门禁、默认供应商不查 key）；P2 16 条（cancel_note 竞态、result.json 非原子写、ffmpeg 无 timeout、note_cache 媒体无上限/Windows 键、插件 userConfig 只覆盖 7/12 等）；P3 8 条（文档代差、CI 一致性无门禁等）。
- 关键结论已代码级核实（default_model 分支、cancel 竞态、preflight 队列、tiktok 映射、模块级单例、Dockerfile COPY、时间戳正则、缓存键冒号）。
- docs/05 追加「第二轮全库扫描」章节 + Wave D 建议落地顺序。

## 小项批（2026-08-17 · 7 项全部落地）

- **下载质量可配置 + yt-dlp 重试**：base.py 写死的 `self.quality` 删除；bilibili postprocessor 按 `quality` 映射码率（fast=32 / medium=64 / slow=128）；新增 `app/downloaders/common.py` 的 `ytdlp_retry`（仅重试网络类错误、指数退避 1.5s×2^i、3 次，业务错误立即抛），6 处 `extract_info` 接入。youtube/generic 保持 `bestaudio` 不转码（转码反而降质）。
- **`.env` 隔离**：6 处 vendored `load_dotenv()` 加守卫（`VIDEONOTE_DATA_DIR` 已设置则不加载），MCP/CLI 启动不再被 CWD `.env` 覆盖。
- **仓库垃圾清理**：删除 `API_BASE_URL` / `BACKEND_PORT` / `BACKEND_BASE_URL` 残留（note.py / config.py / video_helper.py）；截图封面返回 `file://` 绝对路径（不再伪造后端 URL）；顶层 `events/` 迁入 `app/events/`；删除根目录泄漏的旧 `video_note.db`。
- **小宇宙死代码删除**：`xiaoyuzhoufm_download.py` stub + 测试删除（从未接入，yt-dlp generic 已覆盖）。
- **错误形状统一**：`fetch_subtitles` 成功返回 `{ok: true, ...}`。
- **progress 判定**：FastMCP 3.4.5 stdio 下后台线程无 session 可用，推式通知架构不可行；`stage`/`elapsed_secs`（Wave B 已交付）为替代。结论记录在 docs/05 #21。
- kuaishou URL 正则改 raw string，消除 SyntaxWarning。
- 文档：docs/05 标记 #11/#21/#27/#29/#35/#36/#38/#41 状态；VENDOR.md 同步删除与迁移。

## 发布 v0.1.5（2026-08-17）

自 v0.1.4（08-06）以来的变化，全部随本版本发布：

- **插件化 setup**：`/videonote-setup` 命令（体检 → 填 key → 转写 → 默认值 → B站扫码 → 数据管理）、`/plugin configure` userConfig 默认值、install.sh 修「用户级 mcp add 遮蔽插件 env」。
- **正确性（Wave A）**：`file://` 本地路径、未知 task_id → NOT_FOUND、步骤任务写 SUCCESS 并入索引、截图/封面落数据目录（不再泄漏到 CWD）、MCP 拒绝 key/cookie/hf_token、`generate_note` 可省略 `provider_id`。
- **Agent DX（Wave B）**：SKILL 默认全自动少问、`inspect_video` 拆 B 站分 P / 播放列表为可提交 url、`health_check` 带 `server_version`/队列/`skill_refresh`、`get_task_status` 加 `stage`/`elapsed_secs`、`get_task_transcript` 默认截前 50 段、`export_transcript` 回传错误、`merge_audio` 默认写数据目录。
- **跨任务转写缓存（内容寻址）**：同一视频再次生成不重下 + 不重转写；命中时媒体一并复制（audio_path 不悬空）；空转写不进缓存。
- **配置工具**：`delete_provider` / `delete_model` / `test_provider` / `read_app_config`（敏感项过滤）。
- **提交前预检**：`preflight`（ffmpeg / 磁盘 / 转写器 / 供应商 key / 时长）。
- **工程**：ruff F 类全仓清零 + CI 步骤；pytest 套件 170 个全绿；`docs/02` / `VENDOR.md` 与实现对齐。

## 维护（2026-08-17 · CI ruff + 配置工具 + 预检）

- **ruff F 类清理**：`uvx ruff check --select F` 全仓清零（72 处自动修 + 10 处手动：重复 import、未用变量、`import torch` 探测 noqa）。CI 增加该步骤。
- **配置工具补齐**（Phase 2d）：MCP 新增 `delete_provider` / `delete_model(provider_id, model_name)` / `test_provider(provider_id)`（用已存 key 探测，不接受 key 参数）/ `read_app_config()`（返回 setup 默认值，过滤 hf_token / cookie / api_key 等敏感项）。`skills/videonote/reference/tools.md` 同步。
- **提交前预检**（Phase 2，审计 #37）：新增 `preflight(url?, platform?, provider_id?)` —— ffmpeg / 磁盘剩余（≥1GB）/ 转写器就绪 / 供应商 key+模型（与 generate_note 解析一致）/ 队列，url 非空时预解析时长（失败不拦截）。
- 测试 `tests/test_server_contracts.py` 新增 12 个（ProviderConfigToolsTest + PreflightTest），套件 158 → 170。

## 维护（2026-08-17 · 内容寻址转写缓存）

同一视频再次 `generate_note` / `prepare_note_material` 不再重下 + 重转写：

- **`app/services/note_cache.py`**：按 `platform:video_id` 缓存上次转写。身份从 URL 预解析（B 站 BV+p、YouTube v=、抖音 / TikTok；本地文件 sha256；b23 短链解析失败、快手 / generic 解析不出则不命中）。
- **转写按来源分键**：`subtitle`（平台字幕，引擎无关）/ `transcriber_type[:model_size]`（本地引擎拼尺寸）——切换引擎或模型尺寸不会误用旧结果。
- **命中路径最省事**：命中 → 把缓存 transcript 拷进 `{task_id}/gen/transcript.json`，下游已有的 `has_transcript → skip_download` 逻辑自动跳过下载与转写（只做元信息提取）。转写完成后按下载器权威 `audio_meta.video_id` promote（bilibili `_pN` 后缀归一化）。
- **淘汰**：无 LRU；`cleanup_all` 连 `note_cache/` 一起清（`task_manifest.get_cache_dir`）。
- **`get_task_status` 加 `stage` + `elapsed_secs`**：返回中文阶段标签（「转写中」）与任务已耗时（`status.json` 首次提交打 `started_at`，`note._update_status` 保留），轮询可报「转写中，已 3 分钟」。
- **媒体缓存 + 命中复制**（修复 audio_path 悬空）：完整下载时把音频一并收进 `note_cache/<ident>/media/`（引擎无关，local 跳过）；命中缓存时从媒体缓存复制到新任务 raw/，`audio_path` 指向真实文件而非悬空路径（`promote_media` / `lookup_media`）。
- **修 `logs/` 目录首启缺失**：`setup_environment()` 现在创建 `data/logs/`（原 server import 时 `open(logs/mcp_stderr.log)` 因目录不存在被 `except: pass` 吞掉，全新数据目录首启 stderr 重定向静默失效）。
- 测试 `tests/test_note_cache.py`（19 个：身份 / 分键 / 命中 / promote / 媒体缓存 / generate 集成），套件 133 → 154。

## 维护（2026-08-17 · Wave B Agent DX + SKILL 少问）

按 [docs/05-优化清单.md](05-优化清单.md) 做 Agent 更好用的一小刀（不发新版本号，仍 0.1.4）：

- **SKILL 默认全自动**：给链接就跑，不再先问全自动/手动、不再列 6 项确认、不主动问后续优化。用户说「手动」才逐项问。handoff 仅当返回 `handoff: true`。
- **`wait_for_note` 已废弃**：不再 `sleep`，立刻返回当前快照；进行中带 `deprecated`。用 `get_task_status` 轮询。
- **`get_task_transcript` 默认前 50 段**（`truncated` 时用 `"50-"` / `"all"`）。
- **`list_models` 形状统一**：`{ok, source, models:[{id, name}]}`（实时 API 与 DB 回退一致）。
- **`health_check.skill_refresh`**：给出 disable + install 刷新命令。
- **`export_transcript`** 把写入失败放进 `errors`；未配置导出格式时默认 `["srt"]`。
- **`merge_audio`** 默认输出 `note_results/merged/`，不再写 `Path.cwd()`。
- 文档 / `reference/tools.md` / docs/02 / docs/04 / docs/05 对齐。

## 维护（2026-08-17 · inspect_video 分 P / 播放列表）

- 新工具 `inspect_video`：只解析、不下载。B 站走 view API 列出 `pages`，YouTube/generic 用 yt-dlp `extract_flat` 列播放列表。
- 每条 `entries[].url` 可直接喂给 `generate_note` / `prepare_note_material`（B 站 `?p=N`，YouTube `watch?v=`）。Agent 按单视频流程处理；多集用 subagent。
- SKILL 规则 3 / 工作流第 2 步：先 inspect，再按 entries 提交。工具数 31 → 32。

## 维护（2026-08-17 · Wave A 落地）

按 [docs/05-优化清单.md](05-优化清单.md) 修正确性 + 可回归（不发新版本号，仍 0.1.4）：

- **步骤任务写 SUCCESS**：`transcribe_media` / `extract_frames` / `summarize_note` 完成后 `_write_status(SUCCESS)`，并 `insert_video_task` 进全局索引。
- **截图目录**：`note.py` 读 `IMAGE_OUTPUT_DIR`（兼容 `OUT_DIR`），默认数据目录，不再写 CWD。
- **`file://`**：`generate_note` / `prepare_note_material` / `diarize_media` 走 `_coerce_local_path`（unquote + Windows 盘符）。
- **未知 task_id → `NOT_FOUND`**（不再假 PENDING）；`wait_for_note` 立刻返回，并标明会阻塞 stdio。
- **密钥红线**：MCP `add_provider` / `update_provider` / `set_downloader_cookie` / `diarize_media` 拒绝 api_key / cookie / hf_token；填 key 走 CLI。`generate_note` / `summarize_note` 可省略 `provider_id`。
- **卫生**：xiaoyuzhou 去掉 import-time HTTP；cookie tempfile `0600` + unlink；抖音不再 print Cookie；`app_config.json` / downloader cookie `chmod 0600`；预处理清理不再二次拼 `_16k`，且不删源文件。
- **可观测**：`health_check` 含 `server_version` / 队列 / keyed_providers / funasr+mlx 是否已装；`__version__` = 0.1.4。
- **测试 / CI**：`tests/test_server_contracts.py` 等契约测试；CI `uv run --with pytest pytest -q`。
- **文档**：重写 `docs/02`、更新 `VENDOR.md` 冻结清单、对齐 31 工具与「最多 3 并发」口径（README / 04 / SKILL tools.md / CLAUDE.md）。

## 维护（2026-08-17 · 全栈优化点审计）

- 对照 MCP 层（31 个工具）、vendored `app/`、Skill/插件、CI 与文档，产出 [docs/05-优化清单.md](05-优化清单.md)（P0–P3 + Wave A/B/C）。
- 最高优先级（随后 Wave A 已修）：步骤任务不写 SUCCESS、`IMAGE_OUTPUT_DIR` 读成 `OUT_DIR`、主入口不认 `file://`、未知 task 假 PENDING、密钥可经 MCP 进对话。
- `docs/00` 索引补 05。

## 维护（2026-08-16 · README 快速开始精简）

- **中文 README 快速开始精简**：步骤 3 收敛为一行 `! videonote setup`（LLM-Key / B 站扫码 / CLI 向导，配置细节交向导与 Configure 页）；删除「插件默认值怎么填」NOTE 块。
- **README_EN 同步**：步骤 3 同口径精简，删除英文「How to fill in the plugin defaults」NOTE。
- **docs/04 保留 Configure 填法说明**：修掉对已删 README NOTE 的失效引用（改为「Configure 页每个字段下方的说明也列出了可填值」），填法细节作为详细手册内容保留。

## 维护（2026-08-16 · setup 整合进 Claude Code /plugin）

把 `videonote setup` 的**默认值类配置**整合进 Claude Code 插件安装体验（凭证类仍走终端，红线不变）：

- **`plugin.json` 加 `userConfig`**：`/plugin install` 时 Claude Code 逐项提示收非敏感默认值（笔记风格 / 截图 / 视频理解 / 评论弹幕 / 转写引擎 / 模型尺寸 / 笔记位置），**API key 不进 userConfig**（K1 决策，key 仍走 `! videonote providers set`）。
- **`marketplace.json` mcpServers 加 `env`**：`${user_config.*}` → `VIDEONOTE_*` / `TRANSCRIBER_TYPE` / `WHISPER_MODEL_SIZE` 注入 MCP server，作为「配置文件缺项时的兜底默认」。
- **`config.py` 新增占位符剔除 + env 解析助手**：`_purge_placeholder_env()` 在 `setup_environment()` 最前剔除 Claude Code 对「用户跳过未填项」透传的字面 `${user_config.x}`（防坏字符串当真实配置）；`env_or` / `env_bool` / `env_int` / `env_json_list` 供「配置文件优先、env 兜底」读取点解析。
- **`server.py` 默认值读取点加 env 兜底**：`generate_note` / `prepare_note_material` / `summarize_note` / `export_transcript` / 自动导出的 style / screenshot / video_understanding / video_interval / include_comments / comments_limit / default_export_formats 全部按「配置文件优先、env 兜底」读取（与 transcriber 现有模式一致）。
- **新插件命令 `/videonote-setup`**（`commands/videonote-setup.md`）：装完配置的显式入口 —— 体检 → 引导填 key → 转写模型下载 → 展示默认值 → B站扫码 → 数据管理。
- **SKILL / reference/tools.md / README（中英）/ docs/04 同步**：凭证命令改为「本会话 `!` 前缀」引导（`! videonote providers set` 隐藏输入、值不过对话；`! videonote login bilibili` 二维码渲染进会话终端）；全自动模式默认来源补 userConfig。
- **install.sh 修混合安装冲突**：旧版同时做用户级 `claude mcp add videonote`（env 为空）和 marketplace 插件安装，前者会遮蔽插件自带 MCP server 的 env —— 改为 marketplace 优先、仅失败时回退用户级 `claude mcp add` + 本地 Skill 链接；docs/04 加「混合安装注意」警示。
- **userConfig 说明补全**：`/plugin configure` 页**全部为键盘输入**（无下拉/开关，schema 无 enum）——布尔项输入 `true`/`false`、目录项输入绝对路径、字符串项直接输入英文 key；`plugin.json` 的 description 逐项写明可填值，README（中英）/ docs/04 补「插件默认值怎么填」说明。
- **防「装完没重启就配置」**：`/videonote-setup` 命令加**第 0 步「确认 MCP 工具已挂载」**——未挂载（`mcp__videonote__*` 不存在，通常是插件装完没重启会话）→ 立即停止并引导重启 / `/mcp` 排查 / `MCP_TIMEOUT` 重试，禁止 agent 用 CLI/读文件/猜数据目录代替 MCP 工具；SKILL 工作流第 1 步同步；docs/04 明确「重启前不要跑 /videonote-setup」。

## 维护（2026-08-06 · 发布 v0.1.4）

v0.1.3 → v0.1.4 的主要变更（稳定安装：`uvx --from git+https://github.com/HuangYincan/VideoNote-MCP@v0.1.4 videonote`）：

- **修复日志误落 CWD/logs**：`app/utils/logger.py` 日志目录改为首次 `get_logger()` 时延迟解析（不再在 import 时锁定），`videonote_mcp/cli.py` 的 `setup_environment()` 提前到 `provider_probe` import 之前——日志从 `~/logs`（CWD）回到数据目录 `~/.local/share/videonote-mcp/logs/`。

## 维护（2026-08-04 · 发布 v0.1.3）

v0.1.2 → v0.1.3 的主要变更（稳定安装：`uvx --from git+https://github.com/HuangYincan/VideoNote-MCP@v0.1.3 videonote`）：

- **context 轻量化**：`get_task_status` / `wait_for_note` 默认返回轻量结果（markdown/note_dir/title，不再把完整转写灌回 context）；新增 `get_task_transcript(task_id, segment_range)` 按需取转写（支持按段切片，长视频分段精修不撑爆 context）。
- **LaTeX 模板换源**：内置 LaTeX 模板换成 Gua927/Latex_Template 的 Math Note / English Article（去除 RUC 校徽水印，LPPL-1.3c 合规，新增 NOTICE.md + LICENSE）。
- **新增 typst 模板** `templates/typst/zju-lab/`：理工科笔记/实验报告/论文风，保留 ZJU 校徽/校名 logo（来自 Starlight0798/typst-zju-lab-template，MIT）。
- **文档**：README / README_EN / docs/04 / output-formats.md 同步新模板与用法；CHANGELOG 记录。

## 维护（2026-08-04 · 新增 typst 模板：zju-lab）

- **新增 typst 内置模板** `templates/typst/zju-lab/`：来自 [Starlight0798/typst-zju-lab-template](https://github.com/Starlight0798/typst-zju-lab-template)（MIT License）—— 理工科笔记 / 实验报告 / 论文风，含封面（课程/学院/姓名/学号/日期 + **保留 ZJU 校徽/校名 logo**）、目录、页眉页脚、两级标题、公式/图表编号、代码高亮、定理环境、参考文献。
- **用法**：`template.typ` / `imports.typ` / `img/` 与 `note.typ` 同目录，`#import "template.typ": project` + `#show: project.with(course: ..., watermark: "ZJU")`；依赖的 `@preview/*` 包由 typst 自动拉取；`typst compile note.typ note.pdf` 编译（可选，无 typst 则只交付 `.typ`）。
- **许可**：MIT 许可文本随模板保留（`LICENSE`），README 注明来源、ZJU logo 保留与适配其它学校方法。
- **文档同步**：`output-formats.md` typst 章节改写为 zju-lab 模板流程；README / README_EN / docs/04 提及 typst 内置模板；CHANGELOG 记录。

## 维护（2026-08-04 · LaTeX 模板换源：去除 RUC 水印）

- **LaTeX 导出模板更换**：内置模板从 4 个单文件风格（academic / lecture / meeting_minutes / minimal）换成 [Gua927/Latex_Template](https://github.com/Gua927/Latex_Template) 的两个子目录 —— `Math Note/`（数学/理工科笔记，`\documentclass{MathNote}`，中文版 `MathNoteCN`，含定理/引理/定义/推论/例题/命题/证明/注记环境）与 `English Article/`（英文文稿/演讲大纲，`\documentclass{article}` + 摘要/章节/多级列表/参考文献）。
- **去除 RUC 校徽水印**：删除两模板中 `\usepackage{background}` + `\backgroundsetup{...logo-RUC.png}` 的整页背景水印（`MathNote.cls` / `MathNoteCN.cls` / `English Article/main.tex`），并删除 `logo-RUC.png`；重编译 `main.pdf`、重新生成 README 预览图（image.png / image-1.png），产物均无水印。
- **LPPL-1.3c 合规**：模板头部加"改编自上游 + 修改说明"注释；新增 `templates/latex/NOTICE.md`（来源/修改内容/许可义务/变更日志）与 `LICENSE-LPPL-1.3c.txt`（许可原文）；模板 README 注明改编来源、修改点与上游链接，并声明由 VideoNote-MCP 维护、与上游作者无关。
- **文档同步**：`output-formats.md` LaTeX 章节改写为两模板流程（含 Math Note 需随带 `.cls`、连续编译两遍的说明）；README / README_EN / docs/04 模板名引用更新为 Math Note / English Article；CHANGELOG 记录。

## 维护（2026-08-04 · 发布 v0.1.2）

v0.1.1 → v0.1.2 的主要变更（详见下方各「维护」节点块；稳定安装：`uvx --from git+https://github.com/HuangYincan/VideoNote-MCP@v0.1.2 videonote`）：

- **品牌重命名**：BiliNote-Mcp → **VideoNote-Mcp**（Python 包 `videonote_mcp`、CLI `videonote`、Skill `skills/videonote`、环境变量 `VIDEONOTE_*`、数据目录 `~/.local/share/videonote-mcp`、GitHub 仓库 `HuangYincan/VideoNote-MCP`）。
- **数据层重构**：每任务一个文件夹 `note_results/{task_id}/`（`raw/` 下载物 + `gen/` 生成物隔离）；SQLite `video_tasks` 全局任务索引（title/status/summary/note_dir）+ 新工具 `list_tasks`；语义标题捕获；setup ④ 数据管理（列任务/清理单任务/全局清理）。
- **FunASR 中文引擎**：Paraformer-zh + VAD + 中文标点（可选重依赖，中文转写质量优于 faster-whisper）。
- **音频增强**：`merge_audio`（多文件合并 16kHz mono）、音频预处理（归一 + 超长分块）、`diarize_media` 说话人分离（pyannote 可选）、generic 下载器（yt-dlp 覆盖 1800+ 站点）。
- **多格式输出**：`export_transcript` 确定性导出 srt/vtt/json（不耗 LLM）+ setup 导出格式默认；LaTeX 模板资产 + output-formats reference（创意格式走 SKILL/Agent）；不支持的平台返回 `handoff:true` 由 Agent 接手。
- **流水线解耦**：独立步骤层 `app/services/pipeline.py` + 4 个新 MCP 工具（`fetch_subtitles` / `transcribe_media` / `extract_frames` / `summarize_note`）。
- **全量审计修复**：转写配置 / YouTube 字幕 / 截图 / 导出 / 路径安全等 28 文件。
- **文档**：README 中英模板化重构 + 头图定稿 + 真实端到端示例（`examples/note-generation-example/`）+ 数据目录路径统一 `~/.local/share/videonote-mcp`。

## 维护（2026-08-02 · 重命名 VideoNote-Mcp）

- **全面重命名**：项目从旧名更名为 **VideoNote-Mcp** —— Python 包 `videonote_mcp`、CLI 命令 `videonote`、Skill `skills/videonote`、环境变量 `VIDEONOTE_*`、数据目录 `~/.local/share/videonote-mcp`、数据库 `video_note.db`、GitHub 仓库 `HuangYincan/VideoNote-MCP`；旧命名体系（`*_mcp` / `-mcp` / `*_NOTE_*` 前缀等）全部替换为 `videonote*`。本条目为文档侧记录，代码侧重命名由并行工作完成。
- **README 模板化重构**：`README.md` / `README_EN.md` 中英按「流水线地图模板」重写 —— 顶部首图占位 + H1/tagline/语言切换 + 锚点 TOC；9 个流水线阶段（0 端到端 → 8 任务管理）各一节（职责段 + 工具表）；新增「流水线地图」总览表、最佳实践、贡献、致谢。
- **docs 归档**：安装（4 种方式）/ 配置 / 使用 / **环境变量表** / **更新** / **安全（API Key）** 等完整说明归档进 `docs/04-使用手册.md`，README 只保留概览与快速开始；`docs/02-架构设计.md` / `docs/03-预期效果.md` / `docs/00-文档索引.md` / `docs/01-目的与背景.md` 旧名引用全部清除并保持中立表述。
- **示例同步**：`examples/note-generation-example/README.md` 品牌、命令（`videonote`）、Skill 路径（`skills/videonote`）、仓库 URL（`HuangYincan/VideoNote-MCP`）同步更新。
- **Obsidian 宣传资料同步**：对外宣传文案中的旧名一并替换为 VideoNote-Mcp。

## 维护（2026-08-02 · 数据层重构：任务索引 + 语义标题 + raw/gen 隔离）

- **每任务一个文件夹（raw/gen 隔离）**：产物从扁平散落（`dl_{task_id}/`、`{task_id}.json`、`{task_id}_transcript.json` 等）重构为 `note_results/{task_id}/` 统一结构——`raw/`（下载媒体/字幕/封面）+ `gen/`（`transcript.json` / `note.md` / `Assets/` 截图 / `frames/` 帧 / 导出 srt·vtt·json）+ `status.json` / `result.json` / `manifest.json` 控制文件。note.py 落盘路径、server 读取点（`get_task_status`/`export_transcript`/`_run_note_task`/`_run_step_task`）、extract_frames save_dir、checkpoint 取舍全部适配。
- **全局任务索引（SQLite `video_tasks` 表扩展）**：加 `title/status/summary/note_dir` 列；`init_db` 用 `PRAGMA table_info` + `ALTER TABLE` 幂等迁移（兼容旧库）；DAO 重构（`insert_video_task` upsert、`update_task_status`、`list_tasks`、`delete_task`）；**修复 material 模式任务不写库的 bug**。
- **语义标题/简介**：`_save_metadata` 捕获 `_extract_note_title(markdown) or audio_meta.title` 作 title、转写前 200 字作 summary，写入全局索引；`get_task_status` result 补 `title`。
- **新 MCP 工具 `list_tasks()`**（工具 29 → **30**）：查全局索引返回 `[{task_id, title, status, summary, platform, created_at, note_dir}]`——Agent 据此枚举任务、按语义标题识别。
- **cleanup 适配新结构**：`task_manifest` 以任务文件夹为边界——`cleanup_note(include_note=False)` 删 `raw/` + `gen/` 内非笔记（保留 note.md + 控制文件），`include_note=True` 删整个任务夹 + 全局索引；`cleanup_all` 同步清空全局索引；新增 `record_task_meta`/`get_task_meta`。
- **setup ④ 数据管理**：向导加「④ 数据管理」——列任务（task_id | 标题 | 状态）、清理单任务（确认保留/连笔记）、全局清理；纯文本兜底同步。
- **测试**：`test_task_manifest`/`test_material_mode` 适配 task_dir 新结构；新增 `test_task_index`（迁移/DAO/list_tasks，隔离 DB）。全量回归绿（80 单测 + test_material_mode）。
- **文档**：docs/04（存储结构与清理 + 工具表）、README 中英（存储章节 + list_tasks 工具表）、SKILL reference/tools.md（任务索引与清理章节）、CHANGELOG 同步。
- **明确不做**：不迁移历史数据（旧扁平文件保留不读，`cleanup_all` 可清）；checkpoint 临时恢复文件暂留扁平层（成功即删，非最终产物）。

## 维护（2026-08-02 · FunASR 中文引擎）

- **FunASR Paraformer-zh 中文转写引擎**（可选重依赖）：新增 `app/transcriber/funasr_transcriber.py` —— `AutoModel(model="paraformer-zh", vad_model="fsmn-vad", punc_model="ct-punc")` 一个 pipeline 端到端输出**带标点**的中文文本 + VAD 段落时间轴（`sentence_info` 毫秒→秒映射为 `TranscriptSegment`）。中文质量优于 faster-whisper（Paraformer-zh WER ~8.4%）。**惰性加载**：模块顶层不 import funasr，未装时抛 RuntimeError 安装指引（复用 mlx/pyannote 可选依赖模式）。
- **注册与配置**：`transcriber_provider` 加 `FUNASR` 枚举 + 单例 + dispatch；`is_model_ready` 对 funasr 未装返回 `ready:false` + 安装指引（模型首次转写自动下载，无需预检模型文件）；pyproject 加 `funasr = ["funasr", "torch"]` extras。
- **CLI/setup 同步**：`videonote transcriber set funasr`、`_TRANSCRIBER_ENGINES` 加 funasr、InquirerPy 向导 + 纯文本兜底引擎列表 + 未装黄色警告 + 安装指引。
- **测试**：新增 `tests/test_funasr.py`（4 项：未装安装指引 / sentence_info 毫秒→秒映射 / 无句信息单段 / 空结果）。全量回归绿（73 单测 + test_material_mode）。
- **文档**：docs/04（转写引擎章节 + funasr 说明）、README 中英（命令 + 引擎列表）、SKILL reference/tools.md（set_transcriber("funasr") + 配置要点 + validate_url generic 更新）、CHANGELOG 同步。

## 维护（2026-08-02 · 音频能力扩展）

- **平台覆盖（generic 下载器）**：新增 `app/downloaders/generic_downloader.py` —— 用 yt-dlp 默认提取器（含 GenericIE 兜底）覆盖内置 5 平台之外的 **1800+ 站点**；`detect_platform` 未知 URL 返回 `"generic"`（不再 `"unsupported"`），`SUPPORT_PLATFORM_MAP` 加 `generic` 键，`validate_url` 对 generic 返回 `{supported:true, platform:"generic"}`。handoff 保留为 yt-dlp 也失败（登录墙/JS 渲染）时的兜底。清理 server.py 死代码 `_PLATFORM_HINTS`。
- **多文件合并**：新增 `app/services/merge.py` + MCP 工具 `merge_audio(files, out_dir?)` —— FFmpeg concat 把多段录音/会议分段/多个本地视频合并为 16kHz mono wav（自动统一转码），再转写/总结。工具 27 → **29**（+merge_audio +diarize_media）。
- **音频预处理**：新增 `app/transcriber/audio_preprocess.py`（`normalize_to_wav` 16kHz mono / `chunk_if_long` 超长分块 / `denoise` 可选 / `preprocess_pipeline`）；`pipeline.transcribe_audio` 插入预处理钩子（`enable_preprocess` 开启时归一+分块转写+时间偏移拼接，**默认关**、零硬依赖）；`transcriber_config_manager` 加 `enable_preprocess`/`diarization`/`diarization_speakers` 配置键。降噪（noisereduce）做成可选 extras，未装静默降级。
- **说话人分离（可选重依赖）**：新增 `app/services/diarization.py`（`diarize_audio` pyannote 3.x + `assign_speakers` 时间对齐）；`TranscriptSegment` 加 `speaker` 字段（默认 None 向后兼容）；MCP 工具 `diarize_media(audio_file, num_speakers?, hf_token?)`；pyproject 加 `diarization = [pyannote.audio, torch, torchaudio]` 与 `preprocess = [noisereduce, scipy]` 两个可选 extras。未装 pyannote / 缺 HF_TOKEN → RuntimeError 带安装指引（复用 mlx 模式）。
- **setup / CLI / health_check 同步**：`_wizard_transcriber` 加「音频预处理」「说话人分离」开关（pyannote 未装给黄色安装指引 + HF_TOKEN 询问）；`_setup_cli_fallback` 纯文本兜底同步；`videonote transcriber preprocess on/off`、`diarization on/off` 子命令；`set_transcriber` 透传新参数；`health_check` 增 `audio_enhance` 块（预处理/分离就绪 + noisereduce/pyannote 是否已装）。
- **测试**：新增 `tests/test_merge.py`（4 项）、`tests/test_audio_preprocess.py`（6 项）、`tests/test_diarization.py`（6 项）；`test_export.py` 平台断言更新为 generic。全量回归绿。
- **文档**：docs/04（工具表 + 音频增强章节 + 故障表）、README 中英（工具表 + 进阶音频增强）、SKILL reference（tools.md 音频增强章节 + 配置要点 + troubleshooting pyannote/generic 项）、CHANGELOG 同步。

## 维护（2026-08-02）

- **多格式输出层（解耦：MCP 机械格式 + SKILL 创意格式）**：
  - **新增 `videonote_mcp/export/` 包**（自有代码，仅确定性机械格式）：`srt.py` / `vtt.py` / `json.py` 纯渲染（时间轴换算，毫秒进位、`-->` 转义、空段兜底），`exporter.py` 落盘 `note_results/{task_id}/` 并记入 task manifest（可被 `cleanup_note` 清理）。
  - **新 MCP 工具 `export_transcript(task_id, formats?, out_dir?)`**（工具 26 → **27**）：读任务转写 → 导出 srt/vtt/json，**不耗 LLM**，同步返回 `{task_id, formats:{fmt:"file://路径"}}`。任务成功后若 setup ③ 配置了「导出格式默认」（`default_export_formats`），自动导出这些格式。
  - **新 CLI 子命令 `videonote export`**：`export list` / `export <task_id> --format srt,vtt,json [--out-dir]`；setup ③ 新增「导出格式默认」多选（checkbox，srt/vtt/json，清空 = 不自动导出）。
  - **创意格式走 SKILL + Agent**：思维导图（Mermaid）/闪卡/LaTeX/typst/**用户自定义模板**由 Agent 基于 MD 底稿生成，不新增 MCP 工具、不耗配置 provider（与 `agent_direct` 同哲学）。**新增 `skills/videonote/templates/latex/`** 4 个 LaTeX 风格模板资产（academic / lecture / meeting_minutes / minimal，frontmatter 元数据 + 占位符），用户选风格后 Agent 按模板生成 `.tex`（可选 `xelatex` 编译 PDF）；**新增 `skills/videonote/reference/output-formats.md`** 具体步骤；SKILL.md 加「🖨 输出格式」地图小节。
  - **平台接手（Agent 解析超范围链接）**：`pipeline.detect_platform` 未知 URL 返回 `"unsupported"`（不再 raise），新增 `pipeline.handoff_result()`；`validate_url` / `generate_note` / `prepare_note_material` 对不支持的平台返回结构化 `{supported:false, ok:false, handoff:true, hint}` —— Agent 读到 `handoff:true` 即用 WebFetch/浏览器/yt-dlp 通用模式接手解析，再以本地文件调用 `platform="local"`。SKILL 强制规则 6 / reference/troubleshooting 同步。
  - **文档**：docs/04（工具表 + 输出格式章节）、README（中英工具表 + 进阶输出格式章节）、CHANGELOG 同步；新增 `tests/test_export.py`（18 项：srt/vtt/json 渲染、exporter 落盘/manifest、detect_platform/handoff）。

## 维护（2026-08-01）

- **流水线模块解耦**：新增独立步骤层 `app/services/pipeline.py`（`fetch_subtitles` / `transcribe_audio` / `extract_frames` / `fetch_comments_danmaku` / `summarize_material` 五个无状态步骤函数），`NoteGenerator.generate()` 内部改为复用它们（`_get_transcript` / `_transcribe_audio` / `_fetch_comments_danmaku` / `_summarize_text` 改薄委托，行为不变）；新增 4 个独立 MCP 工具 —— `fetch_subtitles`（同步，只取字幕）、`transcribe_media`（异步，只做 ASR）、`extract_frames`（异步，本地 mp4 → 关键帧 file://）、`summarize_note`（异步，吃素材包做 LLM 总结）—— 只抓弹幕评论 / 只做语音识别 / 已有字幕+画面理解 / 已有 mp4 画面理解 等任意组合都可用（工具 22 → **26**）。新增 `tests/test_pipeline_steps.py`（14 项：字幕/转写/抽帧落盘/评论聚合/summarize 素材包，全 mock）。SKILL reference / README 中英 / CHANGELOG 同步。
- **README 结构调整**：「真实端到端使用示例」上移到「快速开始（TL;DR）」之后、安装之前（含三份成品笔记的直接链接 + 完整过程记录）；安装章节前新增「以下内容写给 Agent 看，人类可让 Agent 安装」说明；顺手修复「图片插入（便携笔记）」章节一处重复行。README 中英同步。
- **新增真实端到端使用示例 `examples/note-generation-example/`**：一次「3 个 B 站链接 + 输出目录」的极简 Prompt 自动生成三份精修笔记（参数确认 → 多视频并行 → 视频理解截图 → 弹幕/评论整合 → 基于字幕精修），附三份成品笔记（`note.md` 精修版 + `note_original.md` 原版 + `Assets/` 截图）与完整过程记录（`README.md`）。README 中英加「真实端到端使用示例」入口。
- **SKILL 全自动改为「列出完整参数待确认」**：任务开始问「全自动/手动」后，全自动不再静默套默认，而是**先用 setup 默认解析出本次任务将用的完整参数清单一次性列给用户确认**（生成方式/LLM 模型（或选 AGENT 直接生成 `agent_direct`）/ 风格 `default_style` / 视频理解默认 / 评论默认 / 截图默认 / **生成后是否 AGENT 后续优化**）；用户确认即生成，要改某项再以提问方式改，说「你定」用默认。「AGENT 直接生成」改为在**选 LLM 模型阶段**提供（默认用配置 LLM，可选 AGENT 直接生成，不走配置 LLM）。手动模式不变（逐个问）。SKILL.md / reference/tools.md / README（中英）/ docs/04 同步。
- **README 开发版「从 main 切到 dev」教程补全**：切 dev 的 MCP 命令前加 `claude mcp remove videonote`（若先前在 main，先移除插件默认 main MCP 再覆盖 dev，同名 `add @dev` 才生效）。README 中英 / docs/04 同步。
- **SKILL 双模式（全自动/手动）+ AGENT 直接生成**：
  - SKILL 强制规则新增第 0 条：任务开始**必须先问「全自动」还是「手动」** —— 全自动套用 setup 默认（默认模型 / `default_style` / 视频理解默认 / 评论默认 / 截图默认 / `agent_direct` 默认）不逐个问；手动逐个确认（现有流程）。「先确认参数」改为依模式而定（手动问 / 全自动用默认）。
  - 新增**「AGENT 直接生成」分支**（`agent_direct`，默认关）：`prepare_note_material(video_url, video_understanding?, video_interval?, include_comments?, comments_limit?)` 只跑下载→转写→（可选）抽帧→（可选）评论、**不调用配置 LLM**；`get_task_status` 轮询到 SUCCESS 后读素材包（`transcript.full_text` / `frames` / `comments_danmaku`），**AGENT 自己写笔记**（多模态下 Read 看图、问风格、有评论/弹幕时加「观众观点」章节；转写过长按章节分段精修或让用户指定重点）。
  - 新 MCP 工具 `prepare_note_material`：返回 `{kind:"material", title, transcript:{language, full_text, segments}, frames:[file://...jpg], comments_danmaku, video_path, audio_path}`。
  - setup ③ 新增 `default_style`（默认 detailed）/ `default_screenshot`（默认关）/ `agent_direct`（默认关）默认；`generate_note` 不传 style/screenshot/video_understanding/include_comments 即套默认。
  - SKILL / `reference/tools.md` / README（中英）/ docs/04 同步：全自动/手动模式说明 + AGENT 直接生成流程 + `prepare_note_material` 工具参考。
- **新增清理功能（task manifest + 三个 MCP 工具）**：
  - **可追踪**：任务产生的文件路径（下载音频/转写/markdown/status/result JSON、`dl_{task_id}/`、视频、便携笔记目录）由流水线**尽力而为**记入 `note_results/{task_id}.manifest.json`（`app/utils/task_manifest.py` 的 `record_task_paths`/`get_task_paths`，原子写 tmp+replace，失败不阻断生成）。
  - **新 MCP 工具**：`get_task_files(task_id)`（先查后清：manifest 记录 + `{task_id}*` 前缀扫描真实文件）、`cleanup_note(task_id, include_note=False)`（按任务清中间产物，默认保留最终笔记，`include_note=True` 连笔记+manifest 一起删）、`cleanup_all(include_config=False, include_models=False)`（全局恢复出厂：清空 note_results/static/screenshots/logs，默认保留 config/ 与 models/）。
  - **安全**：只删 manifest 记录 / 明确前缀模式（`note_results/{task_id}`、`dl_{task_id}`）的文件，删除前 `resolve()` 校验在数据目录内（防路径穿越），失败逐条跳过并返回统计；数据库 `video_note.db` 不动。
  - 工具共 **22 个**（原 18 + `prepare_note_material` + 3 清理）。SKILL reference / README（中英）/ docs/04 增补「清理与存储」章节与工具参考；新增 `tests/test_task_manifest.py`（9 项：record/dedup、先查后清、按任务清理保留/连删笔记、路径穿越拒绝、全局清理保留/清 config）。
- **README/docs 增补「开发版（dev 分支尝鲜）」**：dev 版安装（MCP `@dev` 覆盖 + marketplace 指 dev）与 main↔dev 切换/恢复命令、CLI 用 dev、共用数据目录等注意事项。README 中英 / docs/04 同步。
- **修「第二个工具调用挂起」（stderr 管道死锁）+ 并发门禁放宽 + subagent 编排**：
  - **根因**：后台任务大量日志/vendored print 写 stderr，Claude Code 客户端未及时排空 → stderr 管道（~64KB）塞满 → 服务器 logging 持锁阻塞 → 事件循环停 → 后续调用挂起。**修复**：MCP server 启动早期把 stderr 重定向到 `data/logs/mcp_stderr.log`（`os.dup2` + `sys.stderr`），协议只用 stdin/stdout，stderr 进文件不影响；实测修复后 stderr 未排空时第二个调用 0.0s 返回。
  - **并发门禁放宽**：从「有进行中任务就拒绝（强制串行）」改为「最多 `VIDEONOTE_MAX_WORKERS`（默认 3）个进行中任务，超出拒绝」—— 允许 subagent 并行提交多视频。
  - **SKILL**：多视频 → 主 agent 对每个视频起一个 subagent（各自 generate_note + 轮询 + 汇报），主 agent 汇总；主 agent 自己不在同一回合连续调用多个 generate_note。
  - README（中英）/ docs/04 / reference 同步。
- **修 MCP 在笔记目录泄漏 config/logs**：三个 CWD 相对路径的创建者 —— ① `server.py` 的 `app.*` 导入在 `setup_environment()` 之前（logger 用 `./logs`）；② `ProxyConfigManager` 硬编码 `config/proxy.json`；③ `WhisperModelRegistry` 硬编码 `config/whisper_models.json`。全部改为尊重 `VIDEONOTE_CONFIG_DIR`/`VIDEONOTE_DATA_DIR`（`server.py` 导入顺序调整 + 两个 config 管理器默认路径改环境变量），任意 CWD 启动都不再在笔记目录/当前目录冒出空的 config/logs。
- **笔记文件夹结构（一篇一夹）+ 评论/弹幕可视化**：
  - 指定 `notes_dir` 时每篇笔记一个文件夹 `<notes_dir>/<笔记标题>/note.md`（标题取 LLM 生成的笔记 H1，回退视频标题；同名冲突加短 task_id 后缀）—— 多篇互不覆盖；`NoteResult.note_dir` 返回真实子文件夹，server 据此报告位置。
  - `include_comments=True` 时 prompt 强制笔记输出「观众观点」章节（总结弹幕/评论区反复出现的观点、补充、纠错；无可总结写「（无）」）—— 之前只是「仅供参考」喂 LLM，笔记里不可见。
  - README（中英）/ docs/04 / CHANGELOG 同步。
- **SKILL 重构（核心精简 + reference 文件夹）**：SKILL 过长导致 agent 注意力分散、跳过「必须先确认参数」。`skills/videonote/SKILL.md` 重写为「⚡ 强制规则（违反=任务失败，含必须先确认参数）+ 紧凑工作流」；工具接口/配置挪到 `reference/tools.md`、故障排查/并发/B站细节挪到 `reference/troubleshooting.md`（agent 按需 Read）。强制「必须先问参数」放到正文最前。
- **MCP 取消任务 + 强制串行**：
  - 新增 `cancel_note(task_id)` 工具：取消进行中/排队任务（协作式 —— `threading.Event`，任务在各阶段边界 + LLM chunk 循环检查；排队任务可 `Future.cancel()` 释放 worker 槽）。`TaskStatus` 加 `CANCELLED`；`wait_for_note` 终止状态含 `CANCELLED`（不再空转超时）。
  - **`generate_note` 强制串行**：同一会话有进行中任务时**直接拒绝**新提交（并行提交多个 `generate_note` 会让 Claude Code 客户端挂起）—— 必须一次一个：提交 → 等到 SUCCESS/FAILED/CANCELLED → 再提交下一个；真正并行请开多个会话。
  - SKILL/README（中英）/docs/04 同步：串行 + cancel_note 说明。
  - 取消异常/助手独立到 `app/exceptions/task.py`（避免 note→gpt_factory→universal_gpt→note 循环导入）。

- **setup 向导 LLM 配置：连通性检测 + 默认模型**：
  - 供应商改为「管理」子菜单：✏ 编辑 key/base_url / 🔌 检测连接 → 列出可用模型 → 设默认 / ← 返回（选中供应商进入，非再点即编辑）。
  - 检测 = OpenAI 兼容 `GET /v1/models`（一次验证 key/base_url 并拿到模型列表，超时 15s）；`/v1/models` 不可用（部分中转站/自建网关）时降级「最小对话请求」chat 探测。
  - **默认模型**持久化到 `config/app_config.json`（`default_model:{provider_id}`，同时 dedup 写回 models 表）；`generate_note` **未指定 `model_name` 时优先用配置的默认模型**，再退 DB 第一条。
  - 新增非交互 `videonote providers test <id> [--default MODEL]`；`providers list` 显示 `默认=` 列。
  - 纯文本兜底向导（无 InquirerPy）同步支持「检测连接 + 选默认」。
  - 新增 `videonote_mcp/provider_probe.py`（`probe_models` / `probe_chat` 唯一 probe 源）；server 的 `_fetch_live_models` 改为委托它（Ollama 等无 key 供应商现在也能实时列模型）。
  - 坑位处理：选择项 name 不含 ANSI（原样显示）；探测用未掩码 key（`get_provider_by_id`）；子菜单左键只退一级；空 key 归一化只影响探测不影响生成。
  - README（配置①/`providers test`/速查表）、docs/04（子菜单 + 默认模型）、SKILL.md（默认模型一行）同步。
- **setup ③ 其他新增「视频理解默认」+ SKILL 强制问参数**：
  - setup ③ 可配**视频理解默认**（开/关 + 帧间隔秒数），持久化 `app_config.json`（`video_understanding` / `video_interval`）。
  - `generate_note` 的 `video_understanding` / `video_interval` 改为 `Optional`：**不传时**自动套用 setup 默认（默认关 / 0→6s）；**显式传入始终覆盖**（向后兼容）。
  - SKILL「确认参数」强化：**没有明确信息前必须问**用户 —— 是否启用视频理解 + 帧间隔秒数都要问；**即使配了默认，本次也要先问**，只有用户说「你定/用默认」才用默认值。
  - 纯文本兜底向导补「③ 其他（视频理解默认）」。
  - README / docs/04（③ 描述 + 视频理解章节）、SKILL.md 同步。
- **SKILL「后续优化」步骤**：生成成功后 agent **必须问用户**是否要根据已生成笔记 + 提取的字幕（`result.transcript` 完整转写）做后续优化（补齐细节/修正不一致/增强结构）；agent 侧精修、**不新增 MCP 工具**；转写过长时如实告知限制并按章节精修；不写回 `note_dir` 原始产物。
- **SKILL 强制问清单扩展 + `extras` 自定义风格**：
  - **笔记风格改为强制提问**：把**真实 9 种风格**（从 `app/gpt/prompt_builder.py` `note_styles` 核对）呈现给用户选 —— `minimal` 精简 / `detailed` 详细 / `academic` 学术 / `tutorial` 教程 / `xiaohongshu` 小红书 / `life_journal` 生活向 / `task_oriented` 任务导向 / `business` 商业风格 / `meeting_minutes` 会议纪要；没有明确信息前不得自行默认 `detailed`。
  - **支持自定义风格**：`generate_note` 新增 `extras` 参数（追加到 prompt 末尾的自定义指令，note.py 本已支持、之前未暴露）—— 用户自定义风格时把描述经 `extras` 传入。
  - **后续优化提前到步骤 4**（与模型/视频理解等并列强制问「生成后要不要基于字幕优化」），步骤 9 强化为「**必须处理，不能跳过**」（已答过则直接执行，没问过则呈现后必须补问）。
  - README / docs/04（工具参考补 `extras`）同步。
- **SKILL 并发流程修正（一次发一个，服务端并发）**：实测证明 MCP server 对并行 `generate_note` 全部 0.01s 返回（3 个并行毫秒级完成），**卡的是 Claude Code 客户端** —— 同一条消息塞多个并行 MCP 工具调用时，最后一个调用的响应收不到、任务也未提交（用户实测 3 集只提交成功 2 集）。修正：**一次发一个 `generate_note`**（拿 task_id 再发下一个），任务照常在服务端并发执行（`VIDEONOTE_MAX_WORKERS=3`）；多任务轮询用轻量 `get_task_status` 快照轮询，不用阻塞的 `wait_for_note`。提交前先告诉用户要依次提交哪些任务。README / docs/04 同步。
- **SKILL 后续优化强调「挖细节、讲透」**：优化执行改为以字幕/转写为权威源 —— **从里面挖出笔记没覆盖的细节、把每个要点展开讲透**（补充背景/原因/步骤/例子/关键数据与结论）；同时**保留原有的「补齐遗漏、修正不一致、增强结构」**三元组。
- **CI + 分支保护 + 版本发布**：
  - 新增 `.github/workflows/ci.yml`：push `main`/`dev` 或 PR 时跑冒烟（`uv sync` + server import + MCP tools/list + CLI），防止坏 commit 上线（`uvx --from git+` 安装直接拉 main，CI 是门禁）。
  - 新增 `.github/workflows/release.yml`：push `v*` tag 自动建 GitHub Release（**tag 驱动发布**）。
  - 新增 `dev` 分支：日常开发走 dev（功能分支 → PR → dev），dev 稳定后 PR → main 发布。
  - `main` 分支保护：要求 PR + CI 绿 + 1 个 review，直接 push 被拒 → **main 永远可用**。
  - 发布 `v0.1.0`（首个稳定版；稳定安装：`uvx --from git+https://github.com/HuangYincan/VideoNote-MCP@v0.1.0 videonote`）。
- **「评论/弹幕整合」配套（setup CLI / SKILL / docs）**：
  - 契约：`generate_note` 新增 `include_comments`（是否整合弹幕+评论区观点）/ `comments_limit`（评论条数，默认 20）；`app_config` 新键 `include_comments`(bool, 默认 False) / `comments_limit`(int, 默认 20)；新增独立工具 `fetch_comments(video_url, limit=20)` / `fetch_danmaku(video_url)`（需 B 站 SESSDATA；抓取失败不阻断笔记）。
  - setup ③ 新增「评论/弹幕整合默认」（开/关 + 评论条数，`max(1,int)` 异常兜底 20），持久化 `app_config.json`；主菜单 ③ 文案、纯文本兜底向导同步补上。
  - SKILL「确认参数」新增**必须问**条目：是否整合弹幕、评论区观点 —— 默认否，要则问条数；需 SESSDATA，没配引导 `videonote login bilibili` 扫码；**即使 setup 配了默认本次也先问**，只有用户说「你定/用默认」才用默认值；配置要点 / 故障排查表补对应行。
  - README / README_EN / docs/04（③ 描述、新增「整合弹幕+评论区观点」章节、工具参考补 `fetch_comments`/`fetch_danmaku` 与 `include_comments`/`comments_limit` 参数）同步。

## 维护（2026-07-31）

- 修复用户侧 MCP 注册：`--from` 需带 `git+` 前缀（`git+https://...`），且用 `claude mcp add --scope user` 注册到用户级。
- **整体重构 README.md**：章节顺序（前提→安装→配置→使用→工具→更新→安全→Skill）、CLI 命令统一简写 + 定义等价形式、key 配置收敛到「对话外 CLI」与安全红线一致、去重、新增安装方式对比表。
- **docs/04-使用手册.md 对齐 README 口径**：key 一律 CLI（`providers set`）、工具参考补 `update_provider`（15 个）、CLI 简写定义、安装方式表格、配置顺序（setup 向导 → LLM → 转写 → Cookie）、Skill 更新命令。
- **视频理解（画面切片）**：
  - 修复 `video_understanding=True` 时 `grid_size` 缺省为空 tuple 导致「视频处理失败」——改为自动默认 `[3,3]`（`screenshot` 模式 `[2,2]`）。
  - README / docs/04 新增「视频理解」章节（`video_understanding` / `video_interval` / `grid_size` 用法、需多模态模型）；docs/04 工具参考补全这些参数；SKILL.md 工作流加「用户想看画面」时的 agent 指引。
- **用户可配置笔记参数 + 图片插入便携笔记（Assets）**：
  - `generate_note` 新增 `notes_dir` 参数（便携笔记位置）；解析优先级：`notes_dir` → `VIDEONOTE_NOTES_DIR` env → `note_results/{task_id}/`。
  - `note.py`：`_insert_screenshots` 支持 `assets_dir`（截图写进 `Assets/`、markdown 用相对引用 `![...](Assets/xxx.jpg)`）；`generate()` 截图模式下写 `note.md` 与 `Assets/` 同层。
  - `server.py`：任务结果返回 `note_dir`。
  - SKILL.md：工作流新增「确认参数」步骤 —— 用户没指定时询问 LLM 模型/转写/风格/是否视频理解/是否插图片+保存位置。
  - README / docs/04：新增「图片插入（便携笔记）」章节。
  - 已单测 `_insert_screenshots` Assets 布局（相对引用 + 图片落盘）。
- **`videonote transcriber` CLI**：终端直接管理语音转写引擎 —— `list` / `set <engine> [--size]` / `download <size>`（本地 whisper 模型下载）；README / docs/04 补命令行。
- README「更新」章节改为**分安装方式表格**，补上 `uv tool install` 装的 CLI 更新命令 `uv tool upgrade videonote`（实测保留 `--with mlx-whisper`）。
- 修复误导提示：`transcriber_config_manager.is_model_ready` 的「请先在设置页下载」改为「请先执行 `videonote transcriber download <size>`」。
- cli.py 本地 whisper 尺寸补上 `large-v3-turbo`（后端早已支持）；README / docs/04 明确转写引擎列表（含 mlx-whisper 仅 macOS）、设备说明（whisper 自动检测 CUDA、CLI download 用 cpu 只因下载不推理）。
- README / docs/04「前提」补：本地 whisper 下载、GPU 加速（NVIDIA 用 `--with torch` 走 CUDA、macOS 用 `mlx-whisper`）。
- **setup 交互升级**：改用 InquirerPy —— 方向键选择 + 高亮、主菜单随时切换、可返回上一步，做成**随时可反复进入修改的配置入口**（① LLM 供应商 ② 转写引擎 ③ 其他/Cookie/笔记位置）。新增 `_download_whisper` 助手（transcriber download 也复用）；无 InquirerPy 时回退纯文本向导；非 TTY 优雅退出。
- **setup UX 打磨**：① 每步清屏 + 彩色/加粗标题（不留历史信息）；② **左键 = 返回上一级**（所有 select/text/secret 绑定 interrupt）；③ 平台 Cookie 改为下拉选择（bilibili/youtube/douyin/kuaishou/其他 + 返回）；④ **默认笔记位置持久化**（`config/app_config.json`，`generate_note` 读取：notes_dir → app_config → env → 默认）；⑤ 本地模型下载流程更清晰（已下载则跳过、未下载才确认）。
- **修复向导崩溃**：InquirerPy 左键绑定写错（缺 `key` 字段 + 用了不存在的 `cancel` action）导致 `KeyError: 'key'` —— 改为 `{"interrupt": [{"key": "left"}]}`（interrupt 是已注册 action，与 Ctrl-C 同效），select/text/secret/confirm 构造验证通过。
- **修复 B 站下载失败**：`bilibili_dm_patch` 未透传 yt-dlp 2026.07.04 新增的 `fatal` 参数导致 `TypeError` —— 已透传，实测用户视频 playinfo 正常。
- **SKILL 确认参数强化**：① LLM 模型 `list_models` 后**列出让用户选**（不悄悄自定）；② 本地转写模型未就绪时**必须问用户**下载或切云端（不静默切换）；③ 故障排查补 B 站 `fatal`/playurl 412 处理。
- **setup 补 mlx-whisper 下载入口**：之前 `_wizard_transcriber` 只在 fast-whisper 分支问下载，mlx 漏了；现本地引擎（fast-whisper/mlx）都检查已下载并询问。`videonote transcriber download` 新增 `--engine mlx-whisper`（macOS）。
- **setup 下载 UX**：① 确认下载后进入**专门「下载 X」界面**（进度条 + 完成停留，按回车返回，不再立刻跳回）；② 下载改用 `snapshot_download` + 自定义 tqdm 进度条（已验证 faster-whisper 能从同一缓存加载）；③ 修「当前尺寸」显示位置（只在当前引擎上显示，不再误标到其它引擎）。
- **修两处向导问题**：① InquirerPy 选择项 `name` 里嵌 ANSI 转义码会原样显示（`^[[1;32m...`）—— 改为纯文本标记；② mlx-whisper 未安装时给出明确指引（`--with mlx-whisper` 装法）而非 `No module named 'mlx_whisper'`（向导检测 mlx 可用性 + `_download_mlx_model` 抛清晰错误）。
- **向导 mlx 缺失不再卡住**：选 mlx-whisper 但环境没装时，显示指引后**主动问「改用 fast-whisper？」**（默认是），确认即切换并继续下载流程，不再「选完引擎没反应」。
- **CLI 参数分发更严**：`videonote` 收到未知参数（如 `--with` 放错位置）时**报错 + 用法提示**，不再静默启动 MCP server；只有**无参数**时才是 MCP server 模式（stdio 客户端启动）。
- **修向导选 mlx 后卡死**：mlx 路径会 `import mlx_whisper_transcriber` → `import mlx_whisper`（加载 MLX 框架很重、可能卡顿）。改为轻量：`check_mlx_whisper_model_exists` 用内联 repo 映射（不 import mlx_whisper，实测 0ms），mlx 可用性用 `find_spec` 判断；加「检查模型状态…」提示。
- **修 mlx 下载失败（numba 循环导入）**：`_download_mlx_model` 之前 import `mlx_whisper_transcriber`（其依赖链 numba 等有循环导入风险）。下载其实只需 `huggingface_hub.snapshot_download` —— 改为用内联 `MLX_REPO_MAP`，纯 HF 下载，不碰 mlx_whisper。
- **`notes_dir` 现在总是写 note.md**：之前只有截图模式才写便携笔记，用户指定 `notes_dir` 却不插图片时笔记不会写入该目录（agent 只能手动提取）。现在指定 `notes_dir`（或截图模式）都会把 `note.md` 写到目标目录，且 `result.note_dir` 总会返回（`_run_note_task` 按 notes_dir → 截图 顺序判断）。SKILL/README 同步。
- **B 站 AI 字幕说明 + 提示**：功能已实现（API 直拉支持 `ai_type`、yt-dlp 兜底 `writeautomaticsub`），但 B 站 AI 字幕需 **SESSDATA cookie**；无 cookie 时 API 返回空列表 → 只能走语音识别。`raw_info.subtitles={}` 只反映手动 CC（AI 字幕在 automatic_captions）。给 `download_subtitles`/`fetch_subtitles` 加了「配置 SESSDATA 即可用 AI 字幕跳过转写」的日志提示；SKILL 故障排查补对应项。
- **B 站扫码登录 `videonote login bilibili`**：终端渲染 ASCII 二维码（qrcode 库）→ 用户 B 站 App 扫码 → 自动轮询 → 提取并保存 SESSDATA（`CookieConfigManager`）。setup 向导「③ 其他」加「B 站扫码登录」选项。SKILL：B 站视频优先 AI 字幕，引导用户扫码/手动；README/docs 补命令与说明。已验证二维码渲染 + SESSDATA 提取保存（mock 测试）。
- **修扫码登录两处**：① 状态码搞反 —— B 站 `86101` 是**未扫码**（安静等待）、`86090` 才是已扫码待确认；② 成功 URL 可能是 **crossDomain ticket**（不带 SESSDATA query）—— 改为用 session 跟随重定向、从 Set-Cookie 提取 SESSDATA。均已 mock 验证。
- **修 SESSDATA 多条 cookie 冲突**：跟随 crossDomain URL 时 B 站会给不同 domain/path 设多条同名 SESSDATA，`requests.cookies.get()` 抛 `CookieConflictError` —— 改为手动遍历取第一条（mock 验证多 cookie 场景）。
- **扫码登录成功/过期后暂留**：成功保存或二维码过期后显示结果并「（按回车返回）」，不再立刻跳回上级菜单（与下载流程一致）。
- **模型管理 UX**：本地模型「已下载」和「下载完成」两种情况都**暂留**，显示模型位置 + 询问是否卸载（新增 `_show_uninstall_option` 助手、`_model_dir` 定位目录），不再一闪而过。
- **并发/多会话说明**：README 新增「环境变量（可选）」表（含 `VIDEONOTE_MAX_WORKERS`）与「多会话并行」说明；SKILL 新增「并发与多会话」章节（任务按 task_id 隔离、每会话默认 3 并发、资源注意）。
- **修 `ready: true` 误报**：`is_model_ready` 只查模型文件、没查环境是否装了对应包（mlx_whisper 可选）→ 文件在但包没装时误报就绪、任务才失败。现在用 `importlib.util.find_spec`（轻量、不 import）检查包可用性，mlx 缺包时 `ready=false` + 清晰原因；`transcriber_provider` 的误导文案（指向不存在的设置页）也改成 CLI 指引。
- **README 快速开始**：方式一下补「插件默认 MCP 不含 mlx-whisper」说明 + 手动覆盖命令（`claude mcp add videonote -- uvx --from ... --with mlx-whisper ...`）及冲突处理（`claude mcp remove` 或改用 `~/.local/bin/videonote`）。
- **修「笔记生成但任务 FAILED」**：`note.py` 的 `_note_dir` 未初始化 —— 未插图片且未指定 `notes_dir` 时，`if _note_dir is not None` 引用未赋值变量 → UnboundLocalError → 生成产物已落盘但任务标记 FAILED。补 `_note_dir = None` 初始化。

## 发布后维护（2026-07-31）

- 首次推送到 GitHub（`HuangYincan/VideoNote-MCP`，PUBLIC）。
- README 补全「一键安装」：仓库地址、clone 步骤、`install.sh` 等价手动步骤。
- **修复打包 bug**：`.gitignore` 的 `models/`/`data/` 无锚点规则误伤 `app/models/`、`app/db/models/`（wheel 缺失，仅本地 editable 安装可用）→ 改为根锚定 `/models/` `/data/`；`pyproject` 加 `requires-python <3.14` 上界（av/faster-whisper 无 3.14 wheel）、wheel 改用 `include` glob。
- 支持 **`uvx --from git+URL` 一键安装**：`claude mcp add videonote -- uvx --from git+https://github.com/HuangYincan/VideoNote-MCP videonote`（已验证，14 个工具全部可用）。
- README 增加「方式一：Claude 命令一行安装」。
- **修复安装后的数据目录 bug**：`videonote_mcp/config.py` 区分「源码 checkout（用仓库 data/）」「已安装包（用 `~/.local/share/videonote-mcp`，不写 site-packages）」；`path_helper.py` 的 `get_data_dir/get_model_dir/get_app_dir` 尊重 `VIDEONOTE_DATA_DIR/VIDEONOTE_MODEL_DIR` 环境变量，并修复上游 `get_data_dir` 返回 `data/data` 的 bug。
- **安装方式定稿**（实测耗时对比）：`uvx`（缓存命中 ~8s、新版 commit ~20s）**自动更新，推荐**；`uv tool install`（~1s 直接启动）固定版本、启动最快。README 以 `uvx` 为方式一。
- README 补充「使用说明」（agent 工作流 + 配置速查）与「Skill」章节（安装、触发方式）。
- **新增 plugin marketplace**：`.claude-plugin/marketplace.json`（Skill + MCP server 一起分发）。安装一条命令：
  `claude plugin marketplace add HuangYincan/VideoNote-MCP && claude plugin install videonote@videonote`。
  - Skill 移到 marketplace 规范路径 `skills/videonote/SKILL.md`；
  - `plugin.json` 故意不写 version → 每次 commit 即新版本（自动更新）；
  - `install.sh` 改用 marketplace 优先、本地链接兜底；
  - `.claude/settings.json` 加入 gitignore（机器本地插件状态不入库）。
  - 修复：仓库根 `.mcp.json` 与 marketplace 的 mcpServers 声明冲突（插件安装时会加载插件根的 `.mcp.json`，注册出错误的 `uv run` server）→ 移到 `examples/mcp.example.json` 作为手动示例。
- **内置供应商预置 + update_provider 工具**：空库启动自动 seed 7 个内置供应商（openai/deepseek/qwen/groq/ollama…，固定 id + 正确 base_url + 空 key），`update_provider(provider_id, api_key)` 填 key；groq 转写器按 id='groq' 找供应商，因此现在可直接用。工具增至 **15 个**。复制 `app/db/builtin_providers.json`（wheel 已确认包含）。
- README 新增「配置示例」：例一 LLM 供应商配置（update_provider + add_model），例二转写引擎切换（本地 whisper / 云端 groq）；docs/04 与 SKILL.md 同步更新。
- **安全修复**：
  1. `get_all_providers_safe` 上游 bug —— 误用 `serialize_provider`（非 safe）导致 `list_providers` 返回完整 api_key → 改为 `serialize_provider_safe`（掩码）。
  2. `update_provider` 日志打印 `filtered_data` 会带 api_key → 打码。
- README 增补：中转站/自建网关配置示例、「没有 LLM key」指南（Ollama 本地免费 / 免费额度注册）、「安全说明」章节（key 存本地 gitignored DB、MCP 响应掩码、明文存储提醒）。
- SKILL.md：前提补充「用户没有 key 优先用 Ollama」的 agent 处理路径。
- **API key 安全通道（对话外）**：新增 `videonote providers` CLI 子命令（`list` / `set` / `add`），用户在终端直接写 key，**key 不经过 agent 对话**（对话会发送到 agent 的 LLM 上游）。README 安全说明改为「别在对话里发 key」指引；SKILL.md 加安全红线（让用户用终端 CLI 填 key）。
- 修复：`builtins.print` 重定向挪到 `import app.*` 之前（douyin_downloader 等模块导入时打印会污染 CLI stdout / MCP stdio）。
- README「更新」章节**修正**：MCP（uvx）确认为自动更新；Skill/插件**不会自动升级**，实测需 `claude plugin disable videonote@videonote` + `claude plugin install videonote@videonote`（`install` 单独执行会因「已安装」被跳过）。
- 前提补充：**uv 为必需**（一行安装命令）；无 uv 走方式四（install.sh 内置 pip 兜底）。
- CLI 命令补充 PATH 无关写法：有 uv 用 `uvx --from ... videonote providers ...`；无 uv 用 `<仓库>/.venv/bin/videonote providers ...`。
- install.sh：skill 安装仅在**有 uv** 时走 marketplace（插件内 MCP 走 uvx），无 uv 自动回退本地链接，避免注册出起不来的 MCP。
- **CLI 轻量化重构**：新增 `videonote_mcp/cli.py`（console script 改指 `cli:main`）。`videonote providers ...` 只导入 provider 相关模块（启动快、无下载器/转写器 import 噪音）；MCP 模式懒加载 `server.py`。修复 CLI 终端输出被导入噪音污染的问题。
- **交互式初始化向导**：新增 `videonote setup` —— 隐藏输入 LLM API key（选内置/中转站供应商）、选语音转写引擎（本地 whisper / groq / bcut / mlx）、选模型尺寸、可选立即下载 whisper 模型。`install.sh` 装完后在交互终端自动唤起。

## 节点 1：仓库脚手架（2026-07-31）

- 新建独立仓库 `VideoNote-Mcp`（git init，分支 main）。
- 初始化目录结构：`videonote_mcp/`（占位）、`app/`（待移植）、`docs/`、`.claude/skills/videonote/`（待建）、`data/`。
- 写入 `pyproject.toml`（基础依赖，待 Phase 5 定稿）、`.gitignore`、`.python-version`(3.12)、`.mcp.json` 示例、`README.md` 骨架、`VENDOR.md`（记录上游 commit `bebf2e8c`）。
- 文档：创建 `docs/00`~`docs/04` 全部中文文档骨架（目的、架构、预期效果、使用手册、索引）。

## 节点 2：核心代码移植（2026-07-31）

- 从上游流水线项目的 `backend/app/` 复制核心流水线模块到 `app/`：downloaders / transcriber / gpt(含 provider) / db(含 models) / models / enmus / exceptions / decorators / validators / services(去 chat/vector_store/model/model_fallback) / utils(去 response/export/minio/ppt) + 顶层 `events/`（转写后清理信号）。
- 应用外科手术改动剥离 FastAPI/Web：`__init__.py` 置空、`services/provider.py`（jsonable_encoder + kombu.uuid → stdlib）、`services/note.py`（删 HTTPException）、`services/transcriber_config_manager.py`（routers.config → 新增 `utils/model_status.py`）。
- 所有文件 `py_compile` 语法通过。
- 文档：更新 `docs/02-架构设计.md`（vendored 边界与改动）、`VENDOR.md`（模块清单 + 同步步骤）。

## 节点 3：MCP 服务（2026-07-31）

- 编写 `videonote_mcp/config.py`（环境/数据目录初始化，先于 `app.*` import）与 `videonote_mcp/server.py`（FastMCP，14 个工具 + 后台任务线程）。
- 补齐遗漏子包：`app/downloaders/douyin_helper/`（ABogus 签名）、`app/downloaders/kuaishou_helper/`；补依赖 `gmssl`、`blinker`。
- 文档：更新 `docs/02`（MCP 层设计与运行时约定）、`docs/04`（工具参考表）、`docs/03`（能力清单对齐）。

## 节点 4：Skill（2026-07-31）

- 编写 `.claude/skills/videonote/SKILL.md`（agent 工作流、安装、配置、故障排查）。
- 文档：更新 `docs/04`（Skill 章节与安装方式）。

## 节点 5：打包与安装（2026-07-31）

- 依据 vendored 模块实际 import 定稿 `pyproject.toml` 运行时依赖（裁剪自 backend/requirements.txt：去掉 FastAPI/uvicorn/chromadb/celery/导出栈等；补 gmssl、blinker、fastmcp）。
- 编写 `install.sh`（venv + 安装 + `claude mcp add` 注册 + 链接 skill）、`videonote` console script、`.mcp.json` 示例。
- 修复 MCP stdio 关键问题：`app/utils/logger.py` 控制台日志改走 **stderr**（stdout 必须只承载 JSON-RPC）；进程级把 vendored 代码里的裸 `print()` 重定向到 stderr。
- 修复 `list_models`/`generate_note` 对 `get_models_by_provider`（返回 dict）的字段访问。
- 文档：更新 `docs/04`（安装/注册/配置/故障排查全流程）、`docs/03`（验收标准收尾）。

## 节点 6：验证（2026-07-31）

- **安装**：`uv sync` 干净安装（Python 3.12），生成 `videonote` console script。
- **MCP stdio**：initialize 握手成功，`tools/list` 返回 14 个工具；日志全部走 stderr，不污染协议。
- **健康检查**：ffmpeg ok、db ok。
- **转写**：`download_transcriber_model("tiny")` 下载成功，whisper tiny 将测试语音正确识别为中文（语言=zh）。
- **端到端**：`generate_note(本地 wav)` 完整跑通 下载→转写→**LLM 步骤**，用假 key 在 SUMMARIZING 干净失败（401 鉴权错误）——证明流水线到 LLM 边界全部可用；因无真实 API key，最终 Markdown 生成未实跑（上游已验证代码）。
- **工具矩阵**：9 项检查全 PASS（health_check / validate_url×4 / set-get_transcriber / 14 工具 / tiny 已下载）。
- **遗留**：`local_downloader` 封面提取对纯音频文件非致命化（改进）；`list_models` 字段访问修复。
- 文档：核对 `docs/` 与实现一致。
