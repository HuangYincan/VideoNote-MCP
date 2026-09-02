import base64
import json
import logging
import os
import re
import shutil
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union
from uuid import uuid4

from dotenv import load_dotenv
from pydantic import HttpUrl

from app.db.video_task_dao import insert_video_task
from app.downloaders.base import Downloader
from app.enmus.exception import NoteErrorEnum, ProviderErrorEnum
from app.enmus.note_enums import DownloadQuality
from app.enmus.task_status_enums import TaskStatus
from app.exceptions.note import NoteError
from app.exceptions.provider import ProviderError
from app.gpt.base import GPT
from app.gpt.gpt_factory import GPTFactory
from app.models.audio_model import AudioDownloadResult, safe_audio_download_result_dict
from app.models.model_config import ModelConfig
from app.models.notes_model import NoteResult
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.services import note_cache, pipeline
from app.services.constant import get_downloader as _new_downloader
from app.services.provider import ProviderService
from app.transcriber.base import Transcriber
from app.transcriber.transcriber_provider import _transcribers, get_transcriber
from app.utils.json_store import write_json_atomic, write_text_atomic
from app.utils.note_helper import prepend_source_link, replace_content_markers
from app.utils.path_helper import get_data_dir
from app.utils.screenshot_marker import extract_screenshot_timestamps
from app.utils.task_manifest import record_task_paths
from app.utils.url_safety import sanitize_error_text, sanitize_url
from app.utils.video_helper import generate_screenshot
from app.utils.video_reader import VideoReader

# ------------------ 环境变量与全局配置 ------------------

# 从 .env 文件中加载环境变量
# 仅上游独立运行时加载 CWD .env；MCP/CLI 环境下 setup_environment 已设好
# VIDEONOTE_DATA_DIR，此时 .env 可能覆盖数据目录等关键配置，必须跳过
if not os.environ.get("VIDEONOTE_DATA_DIR"):
    load_dotenv()

# 输出目录（用于缓存音频、转写、Markdown 文件，以及存储截图）
# 缺省统一落数据目录（#127 B2）：与 task_manifest.get_note_dir 同源——
# 否则不经 config 的裸脚本把产物写 CWD/note_results，清理/status 按数据目录找 → 失明
NOTE_OUTPUT_DIR = Path(os.getenv("NOTE_OUTPUT_DIR", str(Path(get_data_dir()) / "note_results")))
NOTE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
# 截图目录：优先 IMAGE_OUTPUT_DIR（config.setup_environment 设置），
# 兼容上游残留的 OUT_DIR；都没有则落到数据目录，绝不写 CWD。
_default_screens = Path(os.getenv("VIDEONOTE_DATA_DIR", ".")) / "static" / "screenshots"
IMAGE_OUTPUT_DIR = os.getenv("IMAGE_OUTPUT_DIR") or os.getenv("OUT_DIR") or str(_default_screens)
# 图片基础 URL（用于生成 Markdown 中的图片链接，需前端静态目录对应）
IMAGE_BASE_URL = os.getenv("IMAGE_BASE_URL", "/static/screenshots")

# 日志配置
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _extract_audio_from_video(
    video_path: str,
    out_dir: Union[str, Path],
    cancel_event: Optional[threading.Event] = None,
) -> str:
    """从本地视频文件提取音频轨（mp3），供「视频已下载」场景复用（docs/05 #33）。

    截图/视频理解模式已经下载了完整视频，转写不再第二次网络下载音频，
    直接从视频提取。失败抛 RuntimeError（调用方回退常规音频下载）。
    取消时 terminate 子进程而非等 600s 超时（docs/05 第 16 轮 B1）。
    """
    import subprocess
    import time as _time

    src = Path(video_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{src.stem}_audio.mp3"
    tmp_out: Optional[Path] = None
    proc = None
    process_finished = False
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{src.stem}_audio-", suffix=".mp3", dir=out_dir
        )
        os.close(fd)
        tmp_out = Path(tmp_name)
    except OSError as exc:
        raise RuntimeError(f"创建 ffmpeg 临时音频文件失败: {src.name}") from exc
    cmd = [
        "ffmpeg", "-y", "-i", str(src), "-vn",
        "-acodec", "libmp3lame", "-q:a", "4", str(tmp_out),
        "-hide_banner", "-loglevel", "error",
    ]
    try:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except OSError as exc:
            raise RuntimeError(f"启动 ffmpeg 提取音频失败: {src.name}") from exc
        deadline = _time.monotonic() + 600
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                process_finished = True
                raise TaskCancelledError("任务已取消")
            if _time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                process_finished = True
                raise RuntimeError(f"从视频提取音频超时: {src.name}")
            _time.sleep(0.2)
        process_finished = True
        _, stderr = proc.communicate()
        if proc.returncode != 0 or not tmp_out.is_file():
            raise RuntimeError(
                f"从视频提取音频失败: {src.name}（{(stderr or '').strip()[:300]}）"
            )
        # 只有完整转换成功后才替换最终路径；ffmpeg 中断/取消不会留下半成品
        # <stem>_audio.mp3，也不会覆盖已有的完整缓存。
        tmp_out.replace(out)
        return str(out)
    finally:
        # 处理异常/取消时尽量收尾子进程，再删除唯一临时文件。正常成功时
        # replace() 已经移走临时文件，unlink 是幂等兜底。
        if proc is not None and not process_finished and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - 清理路径不能覆盖原始异常
                try:
                    proc.kill()
                    proc.wait()
                except Exception:
                    pass
        if tmp_out is not None:
            try:
                tmp_out.unlink(missing_ok=True)
            except OSError:
                pass


from app.exceptions.task import (
    OfficialTranscriptFetchError,
    TaskCancelledError,
)
from app.exceptions.task import (
    check_cancel as _check_cancel,
)


def task_dirs(task_id: str):
    """每任务一个统一文件夹：{task_dir}/raw（下载媒体）+ {task_dir}/gen（生成物）+ 控制文件。

    数据层重构：不再有扁平 {task_id}.json / dl_{task_id} 等散落文件，
    一个任务的所有内容都在 note_results/{task_id}/ 下。
    返回 (task_dir, raw_dir, gen_dir)。
    """
    task_dir = NOTE_OUTPUT_DIR / str(task_id)
    return task_dir, task_dir / "raw", task_dir / "gen"


def _extract_note_title(markdown: str) -> Optional[str]:
    """从生成的 markdown 提取 LLM 起的标题（第一个 H1/# 行），用于文件夹命名。"""
    for line in (markdown or "").splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip(" #")
    return None


def _reserve_portable_dir(title: Optional[str], task_id: str, base: Path) -> Path:
    """给便携笔记分配一个目录：优先 LLM/视频标题清洗后命名；同名已被占用时回退加短 task_id 后缀。

    标题清洗：替换路径非法字符 + 截断 60，防路径穿越/超长。
    用 `mkdir(exist_ok=False)` 原子占用——`exists()` 预检是 TOCTOU：两个并发任务
    （同 notes_dir + 同标题）都看到「不存在」选同一目录，然后互相 rmtree 对方
    的 Assets/（#123 B7）。`FileExistsError` 命中即换后缀重试。
    """

    def _try(name: str) -> Optional[Path]:
        try:
            target = base / name
            target.mkdir(parents=True, exist_ok=False)
            return target
        except FileExistsError:
            return None

    if title:
        safe = re.sub(r'[\\/:*?"<>|]', "_", str(title)).strip(" .")[:60]
        if safe:
            hit = _try(safe)
            if hit is not None:
                return hit
            hit = _try(f"{safe}-{task_id[:6]}")  # 同名冲突 → 加短 task_id 后缀
            if hit is not None:
                return hit
    hit = _try(task_id)
    if hit is not None:
        return hit
    # 极端兜底：标题 + 后缀 + task_id 全被占（同任务重跑/哈希碰撞）→ 再拼随机段
    return _try(f"{task_id}-{uuid4().hex[:4]}")


class NoteGenerator:
    """
    NoteGenerator 用于执行视频/音频下载、转写、GPT 生成笔记、插入截图/链接、
    以及将任务信息写入状态文件与数据库等功能。
    """

    def __init__(self):
        from app.services.transcriber_config_manager import TranscriberConfigManager
        config_manager = TranscriberConfigManager()
        self.model_size: str = config_manager.get_whisper_model_size()
        self.device: Optional[str] = None
        self.transcriber_type: str = config_manager.get_transcriber_type()
        # 惰性初始化：转写器（含 whisper/mlx 模型下载）只在真正需要音频转写时才加载，
        # 避免有平台字幕/缓存的任务也被构造时的模型下载阻塞（见 note._transcribe_audio）。
        self.transcriber: Optional[Transcriber] = None
        self.video_path: Optional[Path] = None
        self.video_img_urls=[]
        # 本次转写的来源（跨任务缓存分键）：None / "subtitle" / engine_key(...)。promote 用。
        self._transcript_engine: Optional[str] = None
        logger.info("NoteGenerator 初始化完成")


    # ---------------- 公有方法 ----------------

    def generate(
        self,
        video_url: Union[str, HttpUrl],
        platform: str,
        quality: DownloadQuality = DownloadQuality.medium,
        task_id: Optional[str] = None,
        model_name: Optional[str] = None,
        provider_id: Optional[str] = None,
        link: bool = False,
        screenshot: bool = False,
        _format: Optional[List[str]] = None,
        style: Optional[str] = None,
        extras: Optional[str] = None,
        include_comments: bool = False,
        comments_limit: int = 20,
        output_path: Optional[str] = None,
        notes_dir: Optional[str] = None,
        video_understanding: bool = False,
        video_interval: int = 0,
        grid_size: Optional[List[int]] = None,
        cancel_event: Optional[threading.Event] = None,
        material_only: bool = False,
        publish_success: bool = True,
    ) -> NoteResult | None:
        """
        主流程：按步骤依次下载、转写、GPT 总结、截图/链接处理、存库、返回 NoteResult。

        :param video_url: 视频或音频链接
        :param platform: 平台名称，对应 SUPPORT_PLATFORM_MAP 中的键
        :param quality: 下载音频的质量枚举
        :param task_id: 用于标识本次任务的唯一 ID，亦用于状态文件和缓存文件命名
        :param model_name: GPT 模型名称
        :param provider_id: 模型供应商 ID
        :param link: 是否在笔记中插入视频片段链接
        :param screenshot: 是否在笔记中替换 Screenshot 标记为图片
        :param _format: 包含 'link' 或 'screenshot' 等字符串的列表，决定后续处理
        :param style: GPT 生成笔记的风格
        :param extras: 额外参数，传递给 GPT
        :param include_comments: 是否抓取 B 站弹幕与热门评论并作为参考注入 prompt（仅对 B 站视频生效）
        :param comments_limit: 抓取评论条数上限，仅在 include_comments 为 True 时生效
        :param output_path: 下载输出目录（可选）
        :param video_understanding: 是否需要视频拼图理解（生成缩略图）
        :param video_interval: 视频帧截取间隔（秒），仅在 video_understanding 为 True 时生效
        :param grid_size: 生成缩略图时的网格大小，如 [3, 3]
        :param material_only: 只产出素材包（转写/帧/评论/音视频路径），跳过 LLM 总结与写库，返回 NoteResult.material
        :param publish_success: 是否在本流程发布 SUCCESS；MCP 编排传 False，待 result.json 与 manifest 落盘后再发布
        :return: NoteResult 对象，包含 markdown 文本、转写结果和音频元信息
        """
        if grid_size is None:
            grid_size = []

        try:
            logger.info(f"开始生成笔记 (task_id={task_id})")
            # 重置实例状态：NoteGenerator 若被复用跑第二个任务，不清会串上一个任务的数据
            self.video_path = None
            self.video_img_urls = []
            self.transcriber = None
            self._transcript_engine = None
            self._update_status(task_id, TaskStatus.PARSING)

            # format 声明截图与布尔开关归一化：format 含 "screenshot" 等价于 screenshot=True。
            # 双向闭合：server 层把布尔并入 format（#120），这里把 format 回并布尔，
            # 下游 need_full_download / _download_media.need_video 只认布尔也能拿到一致结论
            if "screenshot" in (_format or []):
                screenshot = True
            # 反向自闭合（#129 B5）：screenshot=True 但 _format 未声明时，把 "screenshot"
            # 并入 format——否则直接调 note.generate(screenshot=True, _format=None) 时
            # _post_process_markdown 因 `if _format:` 跳过，markdown 里 *Screenshot-[mm:ss]
            # 标记原样残留（vendored 核心公开的 screenshot 参数半生效）
            if screenshot and "screenshot" not in (_format or []):
                _format = [*(_format or []), "screenshot"]

            # 获取下载器与 GPT 实例

            downloader = self._get_downloader(platform)
            # material_only 模式不调用 LLM，也不要求配置 provider/model
            gpt = None if material_only else self._get_gpt(model_name, provider_id)
            _check_cancel(cancel_event)  # 阶段边界：可取消点

            # 每任务统一文件夹：{task_id}/raw（下载）+ {task_id}/gen（生成）+ 控制文件
            task_dir, raw_dir, gen_dir = task_dirs(task_id)
            gen_dir.mkdir(parents=True, exist_ok=True)
            raw_dir.mkdir(parents=True, exist_ok=True)
            # 缓存文件路径（进 gen/）
            audio_cache_file = gen_dir / "audio.json"
            transcript_cache_file = gen_dir / "transcript.json"
            markdown_cache_file = gen_dir / "note.md"
            # 记录主要产物路径到 manifest（尽力而为，失败不阻断生成）
            record_task_paths(task_id, [
                task_dir,
                raw_dir,
                gen_dir,
                audio_cache_file,
                transcript_cache_file,
                markdown_cache_file,
                task_dir / "status.json",
                task_dir / "result.json",
                gen_dir / "checkpoint.json",
            ])
            # 跨任务内容缓存：同一视频（platform:video_id）复用上次的转写，避免重下/重转写。
            # 命中 → 拷进 gen/transcript.json，下游 has_transcript → skip_download 即跳过下载与转写。
            if not transcript_cache_file.exists():
                note_cache.lookup_transcript(
                    str(video_url), platform, self.transcriber_type, self.model_size, transcript_cache_file
                )
            # 1. 获取字幕/转写：优先缓存 → 平台字幕 → 音频转写
            transcript = None

            # 尝试读取缓存
            if transcript_cache_file.exists():
                logger.info(f"检测到转写缓存 ({transcript_cache_file})，尝试读取")
                try:
                    data = json.loads(transcript_cache_file.read_text(encoding="utf-8"))
                    segments = [TranscriptSegment(**seg) for seg in data.get("segments", [])]
                    transcript = TranscriptResult(
                        language=data.get("language"),
                        full_text=data["full_text"],
                        segments=segments,
                        truncated=bool(data.get("truncated", False)),
                    )
                    logger.info(f"已从缓存加载转写结果，共 {len(segments)} 段")
                except TaskCancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"加载转写缓存失败: {sanitize_error_text(e)}")

            # 缓存没有，尝试获取平台字幕
            if transcript is None:
                logger.info("尝试获取平台字幕（优先于音频下载）...")
                try:
                    transcript = downloader.download_subtitles(video_url)
                    if transcript and transcript.segments:
                        logger.info(f"成功获取平台字幕，共 {len(transcript.segments)} 段")
                        write_json_atomic(transcript_cache_file, asdict(transcript))
                        self._transcript_engine = note_cache.SUBTITLE_KEY
                    else:
                        transcript = None
                        logger.info("平台无可用字幕，将下载音频后转写")
                except OfficialTranscriptFetchError:
                    raise
                except TaskCancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"获取平台字幕失败: {sanitize_error_text(e)}，将下载音频后转写")
                    transcript = None

            # 2. 下载音频/视频
            # 有字幕时只提取元信息，不下载音视频文件（除非需要截图/视频理解）。
            # format 直接声明 "screenshot" 也视为需要视频：否则 prompt 注入的标记指令
            # 让 LLM 输出 *Screenshot-[mm:ss]，但 video_path=None 时替换被跳过 → 标记残留
            has_transcript = transcript is not None
            need_full_download = (
                not has_transcript
                or screenshot
                or video_understanding
                or ("screenshot" in (_format or []))
            )
            audio_meta = self._download_media(
                downloader=downloader,
                video_url=video_url,
                quality=quality,
                audio_cache_file=audio_cache_file,
                status_phase=TaskStatus.DOWNLOADING,
                platform=platform,
                output_path=output_path,
                screenshot=screenshot,
                video_understanding=video_understanding,
                video_interval=video_interval,
                grid_size=grid_size,
                skip_download=not need_full_download,
                cancel_event=cancel_event,
            )

            # 3. 如果前面没拿到字幕，走转写流程
            if transcript is None:
                transcript = self._get_transcript(
                    downloader=downloader,
                    video_url=video_url,
                    audio_file=audio_meta.file_path,
                    transcript_cache_file=transcript_cache_file,
                    status_phase=TaskStatus.TRANSCRIBING,
                    task_id=task_id,
                    # 上方 line 287-304 已试过 downloader.download_subtitles；无字幕视频
                    # 再让 _get_transcript 调一次 pipeline.fetch_subtitles 是重复 API 调用
                    skip_subtitle=True,
                    cancel_event=cancel_event,
                )

            # 3.4 转写就绪后 promote 进跨任务缓存（按来源分键：subtitle / 引擎；另存音频媒体）。
            #     本次转写来自 per-task 缓存（_transcript_engine 为 None）时不动作。
            if transcript is not None and audio_meta is not None:
                note_cache.promote_transcript(
                    platform,
                    str(video_url),
                    audio_meta.video_id,
                    self._transcript_engine,
                    transcript_cache_file,
                )
                note_cache.promote_media(
                    platform, str(video_url), audio_meta.video_id, audio_meta.file_path
                )

            # 3.5 抓取 B 站弹幕/热门评论（可选；失败不阻断笔记生成）
            comments_danmaku = None
            if include_comments:
                comments_danmaku = self._fetch_comments_danmaku(video_url, comments_limit)
            _check_cancel(cancel_event)  # 阶段边界：可取消点

            # 3.0 material_only：只组装素材包返回（转写/帧/评论/音视频路径），不调 LLM；仍写全局索引
            if material_only:
                material = self._build_note_material(task_id, audio_meta, transcript, comments_danmaku)
                # 先持久化全局索引，再发布 SUCCESS；否则数据库写失败时会短暂留下
                # 「状态成功但任务不可枚举」的假成功。MCP 编排传 False，最终状态由
                # _run_note_task 在 result.json/manifest 落盘后发布。
                self._save_metadata(
                    video_id=audio_meta.video_id,
                    platform=platform,
                    task_id=task_id,
                    title=(audio_meta.title if audio_meta else None),
                    status="SUCCESS" if publish_success else "",
                    note_dir=str(task_dir),
                )
                if publish_success:
                    self._update_status(task_id, TaskStatus.SUCCESS)
                logger.info(f"素材准备完成 (task_id={task_id})")
                return NoteResult(markdown="", transcript=transcript, audio_meta=audio_meta, material=material)

            # 3. GPT 总结
            markdown = self._summarize_text(
                audio_meta=audio_meta,
                transcript=transcript,
                gpt=gpt,
                markdown_cache_file=markdown_cache_file,
                link=link,
                screenshot=screenshot,
                formats=_format or [],
                style=style,
                extras=extras,
                video_img_urls=self.video_img_urls,
                comments_danmaku=comments_danmaku,
                cancel_event=cancel_event,
            )

            # 4. 截图 & 链接替换
            # 数据层重构：生成物统一进 {task_id}/gen/（note.md 恒写，Assets/ 在 gen/Assets/）
            assets_dir = gen_dir / "Assets"
            _note_dir = gen_dir
            # 文件夹名优先用 LLM 生成的笔记标题（markdown 的 H1），更准；回退视频标题（用于语义元数据）
            folder_title = _extract_note_title(markdown) or (audio_meta.title if audio_meta else None)
            if _format:
                self._update_status(task_id, TaskStatus.FORMATTING)
                markdown = self._post_process_markdown(
                    markdown=markdown,
                    video_path=self.video_path,
                    formats=_format,
                    audio_meta=audio_meta,
                    platform=platform,
                    assets_dir=assets_dir,
                )
            _check_cancel(cancel_event)  # 阶段边界：可取消点

            markdown = prepend_source_link(markdown, str(video_url))

            # 4.5 写出 note.md 到 gen/（恒写；用户指定 notes_dir 时额外写便携副本）
            _note_dir.mkdir(parents=True, exist_ok=True)
            write_text_atomic(_note_dir / "note.md", markdown)
            logger.info(f"笔记已写出: {_note_dir / 'note.md'}")
            record_task_paths(task_id, [_note_dir, _note_dir / "note.md"])
            if notes_dir:
                # 便携模式：额外写一份 <notes_dir>/<标题>/note.md（以标题命名的可读副本）
                try:
                    portable_dir = _reserve_portable_dir(folder_title, task_id, Path(notes_dir))
                    write_text_atomic(portable_dir / "note.md", markdown)
                    record_task_paths(task_id, [portable_dir, portable_dir / "note.md"])
                    # 便携副本的截图：把 gen/Assets/ 一并拷贝，保证相对引用 Assets/... 可读
                    assets_src = gen_dir / "Assets"
                    if assets_src.exists():
                        try:
                            assets_dst = portable_dir / "Assets"
                            if assets_dst.exists():
                                shutil.rmtree(assets_dst)
                            shutil.copytree(assets_src, assets_dst)
                            record_task_paths(task_id, [assets_dst])
                        except Exception as e2:
                            logger.warning(f"拷贝便携笔记截图失败: {sanitize_error_text(e2)}")
                except Exception as e:
                    logger.warning(f"写便携笔记副本失败: {sanitize_error_text(e)}")

            # 5. 保存记录到数据库（全局索引，含语义标题/状态/简介）
            _check_cancel(cancel_event)  # 阶段边界：可取消点
            self._update_status(task_id, TaskStatus.SAVING)
            semantic_title = folder_title or (audio_meta.title if audio_meta else None) or ""
            summary = (transcript.full_text or "")[:200] if transcript else ""
            self._save_metadata(
                video_id=audio_meta.video_id,
                platform=platform,
                task_id=task_id,
                title=semantic_title,
                status="SUCCESS" if publish_success else "",
                summary=summary,
                # note_dir 契约（docs 审计 G2）：note 任务指向 note.md 所在目录 gen/，
                # 与 get_task_status 的 result.note_dir 一致；material 无 note.md 用 task_dir
                note_dir=str(_note_dir) if _note_dir is not None else str(task_dir),
            )

            # 6. 完成
            if publish_success:
                self._update_status(task_id, TaskStatus.SUCCESS)
            logger.info(f"笔记生成成功 (task_id={task_id})")
            return NoteResult(
                markdown=markdown,
                transcript=transcript,
                audio_meta=audio_meta,
                note_dir=str(_note_dir) if _note_dir is not None else None,
            )

        except TaskCancelledError:
            raise  # 取消要透传给上层（MCP 层写 CANCELLED），不能转成 FAILED
        except Exception as exc:
            logger.error("生成笔记流程异常 (task_id=%s)：%s", task_id, sanitize_error_text(exc))
            self._update_status(task_id, TaskStatus.FAILED, message=sanitize_error_text(exc))
            return None

    # ---------------- 私有方法 ----------------

    def _init_transcriber(self) -> Transcriber:
        """
        根据环境变量 TRANSCRIBER_TYPE 动态获取并实例化转写器
        """
        if self.transcriber_type not in _transcribers:
            logger.error(f"未找到支持的转写器：{self.transcriber_type}")
            raise Exception(f"不支持的转写器：{self.transcriber_type}")

        logger.info(f"使用转写器：{self.transcriber_type} / {self.model_size}")
        # 必须把配置的模型尺寸传下去：get_transcriber 不再读环境变量优先，
        # 否则 set_transcriber 配置的 large-v3 会被 WHISPER_MODEL_SIZE=tiny 覆盖。
        return get_transcriber(
            transcriber_type=self.transcriber_type,
            model_size=self.model_size,
        )

    def _get_gpt(self, model_name: Optional[str], provider_id: Optional[str]) -> GPT:
        """
        根据 provider_id 获取对应的 GPT 实例
        :param model_name: GPT 模型名称
        :param provider_id: 供应商 ID
        :return: GPT 实例
        """
        provider = ProviderService.get_provider_by_id(provider_id)
        if not provider:
            logger.error(f"[get_gpt] 未找到模型供应商: provider_id={provider_id}")
            raise ProviderError(code=ProviderErrorEnum.NOT_FOUND,message=ProviderErrorEnum.NOT_FOUND.message)
        logger.info(f"创建 GPT 实例 {provider_id}")
        config = ModelConfig(
            api_key=provider["api_key"],
            base_url=provider["base_url"],
            model_name=model_name,
            provider=provider["type"],
            name=provider["name"],
        )
        return GPTFactory.from_config(config)

    def _get_downloader(self, platform: str) -> Downloader:
        """
        根据平台名称获取对应的下载器实例

        :param platform: 平台标识，需在 SUPPORT_PLATFORM_MAP 中
        :return: 对应的 Downloader 子类实例
        """
        logger.debug(f"实例化下载器 -  {platform}")
        try:
            instance = _new_downloader(platform)
        except ValueError:
            logger.error(f"不支持的平台：{platform}")
            raise NoteError(code=NoteErrorEnum.PLATFORM_NOT_SUPPORTED.code,
                            message=NoteErrorEnum.PLATFORM_NOT_SUPPORTED.message)
        except Exception as e:
            logger.error(f"实例化下载器失败：{sanitize_error_text(e)}")
            raise

        logger.info(f"使用下载器：{instance.__class__.__name__}")
        return instance

    def _update_status(self, task_id: Optional[str], status: Union[str, TaskStatus], message: Optional[str] = None):
        """
        创建或更新 {task_id}.status.json，记录当前任务状态

        :param task_id: 任务唯一 ID
        :param status: TaskStatus 枚举或自定义状态字符串
        :param message: 可选消息，用于记录失败原因等
        """
        if not task_id:
            return

        NOTE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        task_dir, _, _ = task_dirs(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        status_file = task_dir / "status.json"
        data = {"status": status.value if isinstance(status, TaskStatus) else status}
        safe_message = sanitize_error_text(message) if message else ""
        if safe_message:
            data["message"] = safe_message
        # 保留 MCP 层首次提交的时间戳（get_task_status 的 elapsed_secs 用）；任务一旦开始就不变
        try:
            if status_file.exists():
                old = json.loads(status_file.read_text(encoding="utf-8"))
                if old.get("started_at"):
                    data["started_at"] = old["started_at"]
        except Exception as exc:  # noqa: BLE001 —— 尽力而为，但失败必须留痕（elapsed 会失真）
            logger.warning(f"读取旧 status.json 保留 started_at 失败: {sanitize_error_text(exc)}")

        # 同步全局索引（video_tasks.status）——尽力而为，失败不阻断
        try:
            from app.db.video_task_dao import update_task_status

            update_task_status(task_id, data["status"], message=sanitize_error_text(message) if message else "")
        except Exception as e:
            # debug 会让「任务状态不进全局索引」静默——list_tasks 查不到、cleanup 找不到
            logger.warning(f"同步任务状态到全局索引失败: {sanitize_error_text(e)}")

        try:
            # tmp 带唯一后缀（docs/05 第 16 轮 B9/B8）：note 侧与 server 侧双写者
            # 不再共用固定 status.tmp 互相截断；创建即 0600，无权限窗口
            from app.utils.json_store import _unique_tmp, _write_bytes_with_mode

            temp_file = _unique_tmp(status_file)
            _write_bytes_with_mode(
                temp_file, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"), 0o600
            )

            # Atomic rename operation
            temp_file.replace(status_file)


        except Exception as e:
            logger.error(f"写入状态文件失败 (task_id={task_id})：{sanitize_error_text(e)}")
            # 回退不截断原文件：open('w') 会把上次已落盘的终态（可能是 SUCCESS）
            # 永久抹掉——进程重启后内存快照失效，任务只能显示损坏（#125 B3）。
            # 只在无原文可保时才写入错误说明。
            try:
                if not status_file.exists() or status_file.stat().st_size == 0:
                    with status_file.open('w', encoding='utf-8') as f:
                        f.write(f"Error writing status: {sanitize_error_text(e)}")
                else:
                    logger.warning("保留上次已落盘的状态文件（写失败原因: %s）", sanitize_error_text(e))
            except Exception:
                logger.error("写入错误  %s", sanitize_error_text(e))

    def _handle_exception(self, task_id, exc):
        safe_error = sanitize_error_text(getattr(exc, "detail", exc))
        logger.error("任务异常 (task_id=%s): %s", task_id, safe_error)
        self._update_status(task_id, TaskStatus.FAILED, message=safe_error)

    def _download_media(
        self,
        downloader: Downloader,
        video_url: Union[str, HttpUrl],
        quality: DownloadQuality,
        audio_cache_file: Path,
        status_phase: TaskStatus,
        platform: str,
        output_path: Optional[str],
        screenshot: bool,
        video_understanding: bool,
        video_interval: int,
        grid_size: List[int],
        skip_download: bool = False,
        cancel_event: Optional[threading.Event] = None,
    ) -> AudioDownloadResult | None:
        """
        1. 检查音频缓存；若不存在，则根据需要下载音频或视频（若需截图/可视化）。
        2. 如果需要视频，则先下载视频并生成缩略图集，再下载音频。
        3. 返回 AudioDownloadResult

        :param downloader: Downloader 实例
        :param video_url: 视频/音频链接
        :param quality: 音频下载质量
        :param audio_cache_file: 本地缓存 JSON 文件路径
        :param status_phase: 对应的状态枚举，如 TaskStatus.DOWNLOADING
        :param platform: 平台标识
        :param output_path: 下载输出目录（可为 None）
        :param screenshot: 是否需要在笔记中插入截图
        :param video_understanding: 是否需要生成缩略图
        :param video_interval: 视频截帧间隔
        :param grid_size: 缩略图网格尺寸
        :return: AudioDownloadResult 对象
        """
        # audio_cache_file 现为 {task_dir}/gen/audio.json → task_dir 是它的 parent.parent
        task_dir = audio_cache_file.parent.parent
        task_id = task_dir.name
        self._update_status(task_id, status_phase)
        _check_cancel(cancel_event)

        # 每任务下载目录 = {task_dir}/raw（替代旧 dl_{task_id}）
        dl_dir = output_path or str(task_dir / "raw")
        Path(dl_dir).mkdir(parents=True, exist_ok=True)
        # 记录下载目录与音频缓存到 manifest（尽力而为）
        record_task_paths(task_id, [dl_dir, audio_cache_file])

        # 已有缓存，尝试加载
        if audio_cache_file.exists():
            logger.info(f"检测到音频缓存 ({audio_cache_file})，直接读取")
            cached = None
            try:
                data = json.loads(audio_cache_file.read_text(encoding="utf-8"))
                cached = AudioDownloadResult(**data)
            except TaskCancelledError:
                raise
            except Exception as e:
                logger.warning(f"读取音频缓存失败，将重新下载：{sanitize_error_text(e)}")
            if cached is not None:
                # 需要真实音频文件（无字幕/截图/视频理解）时，file_path 缺失或悬空
                # 视为缓存失效（JSON 在但实体没了不能直接返回；None 曾因 falsy 被吞、
                # 转写时 Path(None) 抛误导性 TypeError——#119 置空路径后暴露）
                if not skip_download and (
                    cached.file_path is None or not Path(cached.file_path).is_file()
                ):
                    logger.warning(f"音频缓存无实体文件，将重新下载：{cached.file_path!r}")
                else:
                    return cached

        # 有字幕且不需要截图/视频理解时，只提取元信息不下载文件
        if skip_download:
            logger.info("已有字幕，仅提取视频元信息（不下载音视频）")
            try:
                _check_cancel(cancel_event)
                audio = downloader.download(
                    video_url=video_url,
                    quality=quality,
                    output_dir=dl_dir,
                    need_video=False,
                    skip_download=True,
                    cancel_event=cancel_event,
                )
                # 命中转写缓存时媒体没真下载，audio.file_path 是悬空路径；
                # 从跨任务缓存复制音频到本任务 raw/，audio_path 才有真实文件
                cached_media = note_cache.lookup_media(str(video_url), platform, dl_dir)
                if cached_media:
                    audio.file_path = cached_media
                else:
                    # 只有路径真实悬空（下载器拼出但文件不存在）才置 None（#119）；
                    # skip_download 对本地文件返回真实源路径（LocalDownloader 直接回
                    # video_url），必须保留——#119 的修复缺存在性检查，把真实路径也
                    # 吞成 None（二次跑本地文件素材包 audio_path 恒空，#122 B1）
                    if not audio.file_path or not Path(audio.file_path).is_file():
                        audio.file_path = None
                        logger.info("媒体缓存未命中，audio_path 置空（%s）", sanitize_url(video_url))
                write_json_atomic(audio_cache_file, safe_audio_download_result_dict(audio))
                logger.info(f"元信息提取完成 ({audio_cache_file})")
                return audio
            except TaskCancelledError:
                raise
            except Exception as exc:
                logger.warning(f"元信息提取失败，将尝试完整下载: {sanitize_error_text(exc)}")

        # 判断是否需要下载视频
        need_video = screenshot or video_understanding
        # grid_size 缺省：截图模式 [2,2]；视频理解模式 [3,3]。空 grid_size 会让
        # VideoReader 收到空 tuple 报「视频处理失败」，故统一在这里兜底
        if need_video and not grid_size:
            grid_size = [2, 2] if screenshot else [3, 3]

        frame_interval = video_interval if video_interval and video_interval > 0 else 6
        if need_video:
            try:
                _check_cancel(cancel_event)
                logger.info("开始下载视频")
                video_path_str = downloader.download_video(
                    video_url, output_dir=dl_dir, cancel_event=cancel_event
                )
                self.video_path = Path(video_path_str)
                logger.info(f"视频下载完成：{self.video_path}")
                record_task_paths(task_id, [self.video_path])

                if grid_size:
                    self.video_img_urls = VideoReader(
                        video_path=str(self.video_path),
                        grid_size=tuple(grid_size),
                        frame_interval=frame_interval,
                        unit_width=960,
                        unit_height=540,
                        save_quality=80,
                    ).run()
                else:
                    logger.info("未指定 grid_size，跳过缩略图生成")
            except TaskCancelledError:
                raise
            except Exception as exc:
                logger.error(f"视频下载失败：{sanitize_error_text(exc)}")
                self._handle_exception(task_id, exc)
                raise

        # 视频已在本地时（screenshot/视频理解模式）从视频提取音频，不再第二次网络下载
        # （docs/05 #33：旧实现 download_video + download 各下载一次）。
        # skip_download=True 只做轻量 extract_info（metadata），拿到 title/duration/cover。
        if getattr(self, "video_path", None) and self.video_path and self.video_path.exists():
            try:
                _check_cancel(cancel_event)
                audio = downloader.download(
                    video_url=video_url,
                    quality=quality,
                    output_dir=dl_dir,
                    need_video=True,
                    skip_download=True,
                    cancel_event=cancel_event,
                )
                audio.file_path = _extract_audio_from_video(str(self.video_path), dl_dir, cancel_event)
                audio.video_path = str(self.video_path)
                write_json_atomic(audio_cache_file, safe_audio_download_result_dict(audio))
                logger.info(f"视频下载完成，音频从视频提取（免二次下载）({audio_cache_file})")
                return audio
            except TaskCancelledError:
                raise
            except Exception as exc:
                logger.warning(f"从视频提取音频失败，回退常规音频下载：{sanitize_error_text(exc)}")
                # 不 raise：回退到下方常规下载

        # 下载音频
        try:
            _check_cancel(cancel_event)
            logger.info("开始下载音频")
            audio = downloader.download(
                video_url=video_url,
                quality=quality,
                output_dir=dl_dir,
                need_video=need_video,
                cancel_event=cancel_event,
            )
            write_json_atomic(audio_cache_file, safe_audio_download_result_dict(audio))
            logger.info(f"音频下载并缓存成功 ({audio_cache_file})")
            return audio
        except TaskCancelledError:
            raise
        except Exception as exc:
            logger.error(f"音频下载失败：{sanitize_error_text(exc)}")
            self._handle_exception(task_id, exc)
            raise


    def _get_transcript(
        self,
        downloader: Downloader,
        video_url: str,
        audio_file: str,
        transcript_cache_file: Path,
        status_phase: TaskStatus,
        task_id: Optional[str] = None,
        skip_subtitle: bool = False,
        cancel_event: Optional[threading.Event] = None,
    ) -> TranscriptResult | None:
        """
        优先获取平台字幕，没有则 fallback 到音频转写

        :param downloader: 下载器实例
        :param video_url: 视频链接
        :param audio_file: 音频文件路径（用于 fallback 转写）
        :param transcript_cache_file: 缓存文件路径
        :param status_phase: 状态枚举
        :param task_id: 任务 ID
        :param skip_subtitle: True 时跳过平台字幕获取，直接走音频转写——调用方已试过
            字幕（generate 主路径试过 downloader.download_subtitles）时避免重复调用
            无字幕视频的字幕 API（#123 B1）。
        :param cancel_event: 协作式取消事件；取消不得被 fallback 路径吞掉。
        """
        self._update_status(task_id, status_phase)
        _check_cancel(cancel_event)

        # 已有缓存，直接返回
        if transcript_cache_file.exists():
            logger.info(f"检测到转写缓存 ({transcript_cache_file})，尝试读取")
            try:
                data = json.loads(transcript_cache_file.read_text(encoding="utf-8"))
                segments = [TranscriptSegment(**seg) for seg in data.get("segments", [])]
                return TranscriptResult(
                    language=data.get("language"),
                    full_text=data["full_text"],
                    segments=segments,
                    truncated=bool(data.get("truncated", False)),
                )
            except TaskCancelledError:
                raise
            except Exception as e:
                logger.warning(f"加载转写缓存失败，将重新获取：{sanitize_error_text(e)}")

        # 1. 先尝试获取平台字幕（委托 pipeline 步骤层，返回 asdict dict）
        if not skip_subtitle:
            logger.info("尝试获取平台字幕...")
            try:
                _check_cancel(cancel_event)
                data = pipeline.fetch_subtitles(video_url)
                if data:
                    transcript = TranscriptResult(
                        language=data.get("language"),
                        full_text=data["full_text"],
                        segments=[TranscriptSegment(**seg) for seg in data.get("segments", [])],
                        raw=data.get("raw"),
                    )
                    logger.info(f"成功获取平台字幕，共 {len(transcript.segments)} 段")
                    # 缓存结果（pipeline 返回的 asdict dict，与 asdict(transcript) 等价）
                    write_json_atomic(transcript_cache_file, data)
                    self._transcript_engine = note_cache.SUBTITLE_KEY
                    return transcript
                else:
                    logger.info("平台无可用字幕，将使用音频转写")
            except TaskCancelledError:
                raise
            except Exception as e:
                logger.warning(f"获取平台字幕失败: {sanitize_error_text(e)}，将使用音频转写")

        # 2. Fallback 到音频转写
        _check_cancel(cancel_event)
        return self._transcribe_audio(
            audio_file=audio_file,
            transcript_cache_file=transcript_cache_file,
            status_phase=status_phase,
            cancel_event=cancel_event,
        )

    def _transcribe_audio(
        self,
        audio_file: str,
        transcript_cache_file: Path,
        status_phase: TaskStatus,
        cancel_event: Optional[threading.Event] = None,
    ) -> TranscriptResult | None:
        """
        1. 检查转写缓存；若存在则尝试加载，否则调用转写器生成并缓存。
        2. 返回 TranscriptResult 对象

        :param audio_file: 音频文件本地路径
        :param transcript_cache_file: 转写结果缓存路径
        :param status_phase: 对应的状态枚举，如 TaskStatus.TRANSCRIBING
        :param cancel_event: 协作式取消事件；取消不得被异常 fallback 转成 FAILED。
        :return: TranscriptResult 对象
        """
        # transcript_cache_file 现为 {task_dir}/gen/transcript.json → task_id 是 parent.parent.name
        task_id = transcript_cache_file.parent.parent.name
        self._update_status(task_id, status_phase)
        _check_cancel(cancel_event)

        # 已有缓存，尝试加载
        if transcript_cache_file.exists():
            logger.info(f"检测到转写缓存 ({transcript_cache_file})，尝试读取")
            try:
                data = json.loads(transcript_cache_file.read_text(encoding="utf-8"))
                segments = [TranscriptSegment(**seg) for seg in data.get("segments", [])]
                return TranscriptResult(language=data["language"], full_text=data["full_text"], segments=segments)
            except TaskCancelledError:
                raise
            except Exception as e:
                logger.warning(f"加载转写缓存失败，将重新转写：{sanitize_error_text(e)}")

        # 调用转写器（惰性初始化：到这一步才真正需要转写，此时才加载模型/实例化）
        _check_cancel(cancel_event)
        if self.transcriber is None:
            logger.info(f"首次需要音频转写，惰性初始化转写器：{self.transcriber_type}")
            self.transcriber = self._init_transcriber()
        try:
            _check_cancel(cancel_event)
            logger.info("开始转写音频")
            # 委托 pipeline 步骤层（返回 asdict dict，与 asdict(transcript) 写缓存等价）
            transcript_dict = pipeline.transcribe_audio(audio_file, transcriber=self.transcriber)
            write_json_atomic(transcript_cache_file, transcript_dict)
            self._transcript_engine = note_cache.engine_key(self.transcriber_type, self.model_size)
            # 重建 TranscriptResult，保持返回类型一致（generate 下游仍按对象访问）。
            # truncated 透传（docs/05 第 16 轮 B2）：预处理分块部分失败时笔记/result 不静默降级
            transcript = TranscriptResult(
                language=transcript_dict.get("language"),
                full_text=transcript_dict["full_text"],
                segments=[TranscriptSegment(**seg) for seg in transcript_dict.get("segments", [])],
                raw=transcript_dict.get("raw"),
                truncated=bool(transcript_dict.get("truncated", False)),
            )
            logger.info(f"转写并缓存成功 ({transcript_cache_file})")
            return transcript
        except TaskCancelledError:
            raise
        except Exception as exc:
            logger.error(f"音频转写失败：{sanitize_error_text(exc)}")
            self._handle_exception(task_id, exc)
            raise
        finally:
            # B14（docs/05 第 16 轮）：bcut 是每任务新实例（requests.Session 此前只靠
            # __del__/GC 关闭）；任务结束确定性 close。whisper/funasr/mlx 的 close 会
            # 释放模型引用且实例被 transcriber_provider 缓存复用——不在这里动。
            if self.transcriber is not None and type(self.transcriber).__name__ == "BcutTranscriber":
                try:
                    self.transcriber.close()
                except Exception:  # noqa: BLE001 —— 释放失败不阻断任务收尾
                    logger.warning("bcut 转写器 close 失败")

    def _summarize_text(
        self,
        audio_meta: AudioDownloadResult,
        transcript: TranscriptResult,
        gpt: GPT,
        markdown_cache_file: Path,
        link: bool,
        screenshot: bool,
        formats: List[str],
        style: Optional[str],
        extras: Optional[str],
        video_img_urls: List[str],
        comments_danmaku: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> str | None:
        """
        调用 GPT 对转写结果进行总结，生成 Markdown 文本并缓存。

        :param audio_meta: AudioDownloadResult 元信息
        :param transcript: TranscriptResult 转写结果
        :param gpt: GPT 实例
        :param markdown_cache_file: Markdown 缓存路径
        :param link: 是否在笔记中插入链接
        :param screenshot: 是否在笔记中生成截图占位
        :param formats: 包含 'link' 或 'screenshot' 的列表
        :param style: GPT 输出风格
        :param extras: GPT 额外参数
        :param video_img_urls: 视频截图 URL 列表
        :param comments_danmaku: 观众评论与弹幕文本（可选，注入 prompt 供参考）
        :return: 生成的 Markdown 字符串
        """
        # markdown_cache_file 现为 {task_dir}/gen/note.md → task_id 是 parent.parent.name
        task_id = markdown_cache_file.parent.parent.name
        self._update_status(task_id, TaskStatus.SUMMARIZING)

        # 组装素材包，委托 pipeline 步骤层做 LLM 总结（GPTSource 构造收敛到 pipeline）
        material = {
            "title": audio_meta.title if audio_meta else None,
            "transcript": asdict(transcript) if transcript else None,
            "frames": list(self.video_img_urls),  # 已是 data URI，pipeline 兼容直接透传
            "comments_danmaku": comments_danmaku,
            "video_path": str(self.video_path) if self.video_path else None,
            "audio_path": audio_meta.file_path if audio_meta else None,
        }

        try:
            markdown = pipeline.summarize_material(
                material,
                gpt=gpt,
                style=style,
                extras=extras,
                formats=formats,
                screenshot=screenshot,
                link=link,
                tags=audio_meta.raw_info.get("tags", []) if (audio_meta and audio_meta.raw_info) else [],
                checkpoint_key=task_id,
                cancel_event=cancel_event,
            )
            write_text_atomic(markdown_cache_file, markdown)
            logger.info(f"GPT 总结并缓存成功 ({markdown_cache_file})")
            # 转写不完整显式标注（docs/05 第 16 轮 B2）：不再静默基于残缺素材产出
            if getattr(transcript, "truncated", False):
                markdown = markdown + (
                    "\n\n> ⚠️ 转写不完整：预处理分块转写有部分失败，本笔记基于残缺转写素材生成，"
                    "请核对后使用\n"
                )
                write_text_atomic(markdown_cache_file, markdown)
            return markdown
        except Exception as exc:
            logger.error(f"GPT 总结失败：{sanitize_error_text(exc)}")
            self._handle_exception(task_id, exc)
            raise

    def _fetch_comments_danmaku(
        self,
        video_url: Union[str, HttpUrl],
        comments_limit: int,
    ) -> Optional[str]:
        """
        抓取 B 站弹幕汇总与热门评论，拼成一段提示词文本（委托 pipeline 步骤层）。

        抓取失败（含 fetcher 模块缺失/接口异常）只记日志，返回 None，不阻断笔记生成。

        :param video_url: 视频链接
        :param comments_limit: 抓取评论条数上限
        :return: 拼接好的弹幕+评论文本；失败或无数据时返回 None
        """
        return pipeline.fetch_comments_danmaku(str(video_url), comments_limit)

    def _build_note_material(
        self,
        task_id: Optional[str],
        audio_meta: AudioDownloadResult,
        transcript: TranscriptResult,
        comments_danmaku: Optional[str],
    ) -> dict:
        """组装素材包：转写全文+分段、持久化帧图片（file:// 绝对路径）、评论/弹幕、音视频路径。

        material_only 模式的产物，供 AGENT（Claude Code）直接读取素材自行写笔记：
          - transcript: asdict(TranscriptResult) → {language, full_text, segments: [{start, end, text}]}
          - frames: self.video_img_urls 是 base64 data URI（VideoReader 临时文件已删），
            逐张解码写 NOTE_OUTPUT_DIR/{task_id}/frames/frame_{i}.jpg，material 里给 file:// 绝对路径；
            解码/落盘失败逐张跳过，不阻断整个素材包。
          - video_path / audio_path: 音视频本地文件路径（可能为 None）。
        """
        transcript_dict = asdict(transcript) if transcript else None

        frames: List[str] = []
        if self.video_img_urls:
            # 数据层重构：帧落盘到 {task_dir}/gen/frames/
            task_dir, _, gen_dir = task_dirs(task_id)
            frames_dir = gen_dir / "frames"
            try:
                frames_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                logger.warning(f"创建帧目录失败 (task_id={task_id})，跳过帧持久化: {sanitize_error_text(exc)}")
                self.video_img_urls = []  # 目录都建不了，后续逐张必然失败，直接清空
            for i, data_uri in enumerate(self.video_img_urls, start=1):
                try:
                    if isinstance(data_uri, str) and data_uri.startswith("data:image"):
                        b64 = data_uri.split(",", 1)[1]
                        frame_path = frames_dir / f"frame_{i}.jpg"
                        frame_path.write_bytes(base64.b64decode(b64))
                        frames.append(frame_path.as_uri())
                    else:
                        logger.warning(f"跳过非 data URI 帧 (index={i}): {str(data_uri)[:60]}")
                except Exception as exc:
                    logger.warning(f"帧 {i} 解码/落盘失败，跳过: {sanitize_error_text(exc)}")

        return {
            "title": audio_meta.title if audio_meta else None,
            "transcript": transcript_dict,
            "frames": frames,
            "comments_danmaku": comments_danmaku,
            "video_path": str(self.video_path) if self.video_path else None,
            "audio_path": audio_meta.file_path if audio_meta else None,
        }

    def _post_process_markdown(
        self,
        markdown: str,
        video_path: Optional[Path],
        formats: List[str],
        audio_meta: AudioDownloadResult,
        platform: str,
        assets_dir: Optional[Path] = None,
    ) -> str:
        """
        对生成的 Markdown 做后期处理：插入截图和/或插入链接。

        :param markdown: 原始 Markdown 字符串
        :param video_path: 本地视频路径（可为 None）
        :param formats: 包含 'link' 或 'screenshot' 的列表
        :param audio_meta: AudioDownloadResult 元信息，用于链接替换
        :param platform: 平台标识，用于链接替换
        :param assets_dir: 传了则截图写进该目录、markdown 用相对引用（Assets/…），否则用全局截图目录
        :return: 处理后的 Markdown 字符串
        """
        if "screenshot" in formats and video_path:
            try:
                markdown = self._insert_screenshots(markdown, video_path, assets_dir)
            except Exception as exc:
                logger.warning("截图插入失败，跳过该步骤：%s", exc)

        if "link" in formats:
            try:
                markdown = replace_content_markers(markdown, video_id=audio_meta.video_id, platform=platform)
            except Exception as e:
                logger.warning(f"链接插入失败，跳过该步骤：{sanitize_error_text(e)}")

        return markdown

    def _insert_screenshots(self, markdown: str, video_path: Path, assets_dir: Optional[Path] = None) -> str | None | Any:
        """
        扫描 Markdown 文本中所有 Screenshot 标记，并替换为实际生成的截图链接。

        :param markdown: 含有 *Screenshot-mm:ss 或 Screenshot-[mm:ss] 标记的 Markdown 文本
        :param video_path: 本地视频文件路径
        :param assets_dir: 传了则截图写进该目录、引用为相对路径 Assets/xxx.jpg（便携笔记）；
                           不传则用全局截图目录与 IMAGE_BASE_URL（绝对 URL，向后兼容）
        :return: 替换后的 Markdown 字符串
        """
        matches: List[Tuple[str, int]] = extract_screenshot_timestamps(markdown)
        for idx, (marker, ts) in enumerate(matches):
            try:
                if assets_dir is not None:
                    assets_dir.mkdir(parents=True, exist_ok=True)
                    img_path = generate_screenshot(str(video_path), str(assets_dir), ts, idx)
                    filename = Path(img_path).name
                    # 便携笔记：相对引用，note.md 与 Assets/ 同层
                    img_url = f"Assets/{filename}"
                else:
                    img_path = generate_screenshot(str(video_path), str(IMAGE_OUTPUT_DIR), ts, idx)
                    filename = Path(img_path).name
                    # 构建前端可访问的 URL，例如 /static/screenshots/{filename}
                    img_url = f"{IMAGE_BASE_URL.rstrip('/')}/{filename}"
                markdown = markdown.replace(marker, f"![]({img_url})", 1)
            except Exception as exc:
                logger.error(f"生成截图失败 (timestamp={ts})：{sanitize_error_text(exc)}")
                # 单帧失败只移除该 marker，绝不让整篇笔记作废（返回 None 会让上层
                # write_text(None) 抛 TypeError → 任务 FAILED、笔记整篇丢失）
                markdown = markdown.replace(marker, "", 1)
                continue
        return markdown

    def _save_metadata(
        self,
        video_id: str,
        platform: str,
        task_id: str,
        title: str = "",
        status: str = "",
        summary: str = "",
        note_dir: str = None,
    ) -> None:
        """
        将任务记录写入全局索引（video_tasks 表），含语义标题/状态/简介。

        :param video_id: 视频 ID
        :param platform: 平台标识
        :param task_id: 任务 ID
        :param title: 语义标题（视频标题 / LLM 标题）
        :param status: 任务状态（SUCCESS/FAILED…）
        :param summary: 语义简介（转写前若干字）
        :param note_dir: 任务文件夹路径
        """
        try:
            insert_video_task(
                video_id=video_id,
                platform=platform,
                task_id=task_id,
                title=title,
                status=status,
                summary=summary,
                note_dir=note_dir,
            )
            # title 可能是 None（无标题视频）——裸切片 TypeError 被 except 吞、误报「保存失败」（#127 B10）
            logger.info(f"已保存任务记录到数据库 (video_id={video_id}, platform={platform}, task_id={task_id}, title={(title or '')[:40]!r})")
        except Exception as e:
            logger.error(f"保存任务记录失败：{sanitize_error_text(e)}")
            # 笔记文件已写出但全局索引没有成功持久化时，不能继续把任务报告为
            # SUCCESS；让 generate() 的外层统一写 FAILED，并把数据库异常暴露给
            # MCP/调用方，而不是留下「有笔记但不可枚举」的假成功。
            raise
