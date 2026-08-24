# VENDOR.md — 上游代码来源与移植说明

本仓库的 `app/` 目录是从上游 BiliNote 仓库**复制**而来（而非 pip 依赖），目的是让 MCP 完全自包含、可独立安装运行，不依赖 FastAPI 后端。

## 上游来源

- 上游仓库：https://github.com/JefferyHcool/BiliNote
- 移植来源 commit：`bebf2e8c6142e195a2b8a01525c4c7ba3cf993f8`
- 移植日期：2026-07-31
- 来源路径：`BiliNote/backend/app/`

## 复制了哪些模块

| 子包 | 内容 |
|------|------|
| `app/downloaders/` | base, common, bilibili_downloader, bilibili_dm_patch, bilibili_subtitle, **bilibili_comment**, youtube_downloader, youtube_subtitle, douyin_downloader, kuaishou_downloader, local_downloader, **generic_downloader** + 子包 `douyin_helper/`、`kuaishou_helper/`（xiaoyuzhoufm_download **已删除** 2026-08-17：未接入且 yt-dlp 通用提取已覆盖小宇宙） |
| `app/transcriber/` | base, transcriber_provider, whisper, groq, bcut, kuaishou, mlx_whisper_transcriber, **funasr_transcriber**, **audio_preprocess**, model_download_state, whisper_models |
| `app/gpt/` | base, gpt_factory, universal_gpt, prompt, prompt_builder, request_chunker + `app/gpt/provider/OpenAI_compatible_provider.py`（gpt_factory 依赖）（openai_gpt / deepseek_gpt / qwen_gpt / utils / tools **已删除** 2026-08-17：全仓零引用——gpt_factory 走 OpenAICompatibleProvider 直连，上游直连类死代码） |
| `app/db/` | engine, init_db, provider_dao, model_dao, video_task_dao + `app/db/models/`（models, providers, video_tasks）（sqlite_client **已删除** 2026-08-17：全仓零引用 + CWD 相对 DB 路径死引信） |
| `app/models/` | audio_model, gpt_model, model_config, notes_model, transcriber_model（provide_model / video_record **已删除** 2026-08-17 #132 C10：全库零引用死模块） |
| `app/enmus/` | exception, note_enums, task_status_enums |
| `app/exceptions/` | note, provider, **task**（biz_exception **已删除** 2026-08-18 #134：全仓零引用死类；**不含** exception_handlers —— 仅 FastAPI 用） |
| `app/decorators/` | timeit |
| `app/validators/` | **整个子包已删除**（2026-08-18 #134：video_url_validator 全仓零引用死模块，上游同步时勿重引入） |
| `app/services/` | note, constant, provider, cookie_manager, transcriber_config_manager, proxy_config_manager, **pipeline**, **merge**, **diarization**, **note_cache**（**不含** chat_service / chat_tools / vector_store —— 本仓库不做 RAG；**不含** model / model_fallback —— 仅 routers 使用；**task_serial_executor 已删** —— 2026-08-17 全仓零引用死模块，MCP 用自己的线程池） |
| `app/utils/` | note_helper, video_helper, video_reader, screenshot_marker, logger, path_helper, url_parser, openai_client, env_checker, **task_manifest**, **json_store**, **url_safety** + **本仓库新增** `model_status.py`（见下）（status_code **已删除** 2026-08-17 #132 C10：全库零引用死模块；**不含** response / export / ppt_generator / minio_client） |
| `videonote_mcp/export/` | SRT/VTT/JSON 确定性导出（不在上游 `utils/export.py`） |
| `events/` | **整个子包已删除**（2026-08-17，#130 B2）：signals（blinker `transcription_finished`）+ handlers（转写完成后临时文件清理）是死链——4 个转写器 `on_finish` 调用全部注释、`transcription_finished` 永不触发，server 的 register_handler 纯空转。整链（server 注册点 + events 包 + 4 个 on_finish 方法）已删；若上游恢复该机制需重新引入 |

## 外科手术改动（相对上游）

为了剥离 FastAPI/Web 层，做了以下最小改动：

1. **`app/__init__.py`** — 删除 `from fastapi import FastAPI` 及 app 实例创建，改为空包标记。
2. **`app/services/provider.py`** — 用标准库 `import uuid` + `created_at.isoformat()` 替换 `from fastapi.encoders import jsonable_encoder` 和 `from kombu import uuid`（去掉了 celery/kombu 依赖）。
3. **`app/services/note.py`** — 删除未使用的 `from fastapi import HTTPException` 导入。
4. **`app/services/transcriber_config_manager.py`** — 把对 `app.routers.config` 的延迟 import 改为 `app.utils.model_status`；新增 **`app/utils/model_status.py`**（从 `routers/config.py` 抽取 `_check_whisper_model_exists` / `_check_mlx_whisper_model_exists` 两个纯函数，并补上「是否下载中」的查询）。
5. **`app/downloaders/local_downloader.py`** — 封面提取改为**非致命**（try/except 跳过）：上游对纯音频文件（mp3/wav）会因无法抽帧直接使任务失败，本仓库允许跳过封面继续生成笔记。
6. **`app/services/cookie_manager.py` / `app/services/transcriber_config_manager.py`** — 配置文件默认路径改为 `VIDEONOTE_CONFIG_DIR`（见 `videonote_mcp/config.py`），避免依赖 CWD。
7. **未移植** 的模块：`routers/`、`main.py`、`utils/response.py`、`utils/export.py`、`utils/ppt_generator.py`、`utils/minio_client.py`、`services/chat_*`、`services/vector_store.py`、`services/model.py`、`services/model_fallback.py`、`exceptions/exception_handlers.py` —— 均确认仅 Web 层（routers/main）使用，核心流水线不依赖。

## 已分叉、不要当「4 处补丁」重打的文件

本仓库在 `app/` 上已经长出独立能力。**不要**再按「diff -r 后手打 4 处补丁」同步——必冲突。上游更新只 cherry-pick 下载器/转写器的无分叉文件，或先把分叉抽回 `videonote_mcp/`。

冻结清单（本仓库已改语义，覆盖上游同名文件时必须人工合并）：

- `app/services/note.py`（任务文件夹、`IMAGE_OUTPUT_DIR`、material_only、便携笔记）
- `app/services/provider.py` / `app/db/video_task_dao.py` / `app/db/models/video_tasks.py`
- `app/services/transcriber_config_manager.py` / `app/utils/model_status.py` / `app/utils/path_helper.py` / `app/utils/logger.py`
- `app/services/cookie_manager.py` / `proxy_config_manager.py` / **`app/utils/json_store.py`**（2026-08-17 #106：三个配置管理器改 `json_store` 安全读写——损坏不静默当空（warning + `.corrupt` 备份）、`_write` 原子化（tmp+replace+0600）；上游若带原生读写逻辑需人工合并）
- `app/downloaders/bilibili_downloader.py` / `generic_downloader.py` / `local_downloader.py`
- `app/downloaders/common.py` / `app/utils/url_parser.py` / `app/downloaders/douyin_downloader.py` / `app/downloaders/kuaishou_helper/kuaishou.py` / `app/downloaders/bilibili_subtitle.py`（2026-08-25 #140：出站请求逐跳 SSRF 校验——stream_download 入口校验 + 短链解析/视频页跟随/API 资源 URL 改走 `url_safety.public_get/public_head`；上游若带原生 requests 调用逻辑需人工合并）
- `app/transcriber/transcriber_provider.py`（funasr / mlx）
- `app/gpt/universal_gpt.py`（checkpoint 损坏弃用时打 warning 留痕，#106）
- 整文件为本仓库新增：`pipeline.py`、`merge.py`、`diarization.py`、`inspect.py`、`note_cache.py`、`audio_preprocess.py`、`funasr_transcriber.py`、`generic_downloader.py`、`bilibili_comment.py`、`task_manifest.py`、`json_store.py`、`url_safety.py`

## 如何同步上游更新

```bash
# 1. 记下当前 vendored 版本
git -C /path/to/BiliNote rev-parse HEAD

# 2. 只 diff 未分叉的文件（下载器适配 / whisper 实现等）
#    不要整树覆盖 app/

# 3. cherry-pick 单文件，对照上面的冻结清单人工合并
# 4. 更新本文件的 commit 号与日期
# 5. 在 docs/CHANGELOG.md 记一条「同步上游」
```

## 许可标注（docs/05 #45）

- `app/utils/abogus.py`（抖音 ABogus 签名）来自 **TikTokDownloader**，作者 JoeanAmier，
  **GPL-3.0** 许可 —— 与本仓库 MIT 混合分发。保留文件头出处声明；整体分发时如需
  规避 GPL 传染，可替换为纯 Python 实现或仅在独立进程调用。
- 其余 `app/` vendored 代码按上游 BiliNote 许可分发。
