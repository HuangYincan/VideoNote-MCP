"""pipeline.py —— 视频流水线独立步骤层。

把 NoteGenerator.generate() 的整体编排拆成**可独立调用**的无状态步骤函数。
每个函数一个职责、输入输出明确；MCP 工具层与 generate() 共用同一套实现，支持任意组合：

  - `fetch_subtitles`      : 只取平台字幕（不下载、不转写）
  - `transcribe_audio`     : 只做语音识别（ASR，给定音频/视频文件）
  - `extract_frames`       : 只抽视频关键帧（画面理解素材，给定本地 mp4）
  - `fetch_comments_danmaku`: 只抓 B 站弹幕 + 评论区观点
  - `summarize_material`   : 只做 LLM 总结（吃素材包，给定转写/帧/评论）

步骤间用「素材包」material dict 传递（与 note.py._build_note_material 一致）：
  `{title, transcript, frames[file://...], comments_danmaku, video_path, audio_path}`

安全纪律：只读不写（除 extract_frames 持久化帧到 save_dir）；不碰状态机/缓存/DB，
那些属于编排层（generate）的职责。
"""
from __future__ import annotations

import base64
import logging
import os
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Union
from uuid import uuid4

from app.downloaders.base import Downloader
from app.exceptions.task import OfficialTranscriptFetchError
from app.gpt.base import GPT
from app.models.gpt_model import GPTSource
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.services.constant import get_downloader as _new_downloader
from app.transcriber.base import Transcriber
from app.transcriber.transcriber_provider import _transcribers, get_transcriber
from app.utils.path_helper import get_data_dir
from app.utils.video_reader import VideoReader

logger = logging.getLogger(__name__)

# 缺省统一落数据目录（#127 B2）：与 task_manifest.get_note_dir 同源，避免 CWD 相对分裂
NOTE_OUTPUT_DIR = Path(os.getenv("NOTE_OUTPUT_DIR", str(Path(get_data_dir()) / "note_results")))

_PLATFORM_HINTS = [
    ("bilibili", ("bilibili.com", "b23.tv")),
    ("youtube", ("youtube.com", "youtu.be")),
    ("douyin", ("douyin.com",)),
    ("tiktok", ("tiktok.com",)),
    ("kuaishou", ("kuaishou.com", "gifshow.com")),
    ("xiaoyuzhou", ("xiaoyuzhoufm.com", "xiaoyuzhou.fm")),
    ("xiaohongshu", ("xiaohongshu.com", "xhslink.com", "xhslink.cn", "rednote.com")),
]


# ---------------- 平台 / 引擎 ----------------

def _match_platform_host(u: str) -> Optional[str]:
    """基于 host 精确匹配平台（含子域名/端口/无协议 URL）。

    旧的子串匹配（`"bilibili.com" in u`）会把 evilbilibili.com、bilibili.com.evil.com
    误判成 bilibili；这里按 host == 目标 或 host 以 `.目标` 结尾判断。
    """
    from urllib.parse import urlparse

    s = u if "://" in u else f"http://{u}"
    try:
        host = urlparse(s).netloc.lower().split(":")[0].rstrip(".")
    except Exception:
        return None
    if not host:
        return None
    for platform, needles in _PLATFORM_HINTS:
        if any(host == n or host.endswith("." + n) for n in needles):
            return platform
    return None


def detect_platform(url: str) -> str:
    """从 URL / 本地路径识别平台（与 server._detect_platform 一致）。

    未知 URL 返回 `"generic"`——走 yt-dlp 通用提取器（覆盖 1800+ 站点，含 GenericIE 兜底）。
    只有 yt-dlp 也解析失败时，调用方才用 handoff_result 把任务交给 Agent 接手。
    空 url 仍 raise ValueError。
    """
    u = (url or "").strip().lower()
    if not u:
        raise ValueError("url 为空")
    if u.startswith(("file:", "/", "./", "../", "~/")) or Path(u).expanduser().exists():
        return "local"
    return _match_platform_host(u) or "generic"


def handoff_result(url: str, reason: str = "") -> dict:
    """构建「yt-dlp 也无法解析 → 交给 Agent 接手」的结构化结果。

    供 server 层（inspect_video / generate_note / prepare_note_material）在 generic
    下载失败（登录墙 / JS 渲染难题）时返回。Agent 读到 `handoff: True` 就知道要
    自行解析：用 WebFetch / 浏览器读取页面提取视频源，或手动处理登录后以本地文件调用。
    """
    return {
        "ok": False,
        "platform": "unsupported",
        "url": url,
        "reason": reason or "yt-dlp 无法解析该链接（可能需登录/JS 渲染/受保护）",
        "handoff": True,
        "hint": (
            "内置平台（bilibili/youtube/douyin/tiktok/kuaishou/xiaoyuzhou/xiaohongshu/本地文件）之外用 yt-dlp "
            "通用提取也失败了。请用 WebFetch/浏览器读取页面提取视频源，或处理登录/验证后"
            "以本地文件调用（generate_note platform='local'）。"
        ),
    }


def get_downloader(platform: str) -> Downloader:
    """按平台惰性创建下载器实例（每次新建，cookie 文件由 __del__/atexit 清理）。"""
    return _new_downloader(platform)


def build_transcriber() -> Transcriber:
    """按当前转写器配置实例化转写器（与 note.py._init_transcriber 一致）。"""
    from app.services.transcriber_config_manager import TranscriberConfigManager

    mgr = TranscriberConfigManager()
    ttype = mgr.get_transcriber_type()
    if ttype not in _transcribers:
        raise ValueError(f"不支持的转写器：{ttype}")
    return get_transcriber(
        transcriber_type=ttype,
        model_size=mgr.get_whisper_model_size(),
    )


# ---------------- 步骤 1：平台字幕 ----------------

def fetch_subtitles(video_url: str, platform: Optional[str] = None) -> Optional[dict]:
    """只取平台字幕（人工/自动字幕），不下载音视频、不转写。

    返回 TranscriptResult 的 asdict（{language, full_text, segments}）；无字幕/失败返回 None。
    """
    if platform is None:
        platform = detect_platform(video_url)
    try:
        tr = get_downloader(platform).download_subtitles(video_url)
        if tr and getattr(tr, "segments", None):
            return asdict(tr)
    except OfficialTranscriptFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 字幕失败不阻断
        logger.warning(f"获取平台字幕失败 platform={platform}: {exc}")
    return None


# ---------------- 步骤 2：语音识别（ASR） ----------------

def apply_diarization(
    audio_file: str,
    segments: List,
    wav_path: Optional[str] = None,
) -> List:
    """配置启用说话人分离时给转写段打 speaker；未启用/失败原样返回（docs/05 #31）。

    接入点：pipeline.transcribe_audio 转写完成后调用一次，generate_note 的
    note.py 路径与 transcribe_media / prepare_note_material 全部自动生效。
    自己归一化产生的临时 wav（_16k.wav）在 finally 中清理。
    """
    try:
        from app.services.transcriber_config_manager import TranscriberConfigManager

        mgr = TranscriberConfigManager()
        if not mgr.get_diarization():
            return segments
        from app.services.diarization import assign_speakers, diarize_audio
        from app.transcriber.audio_preprocess import normalize_to_wav

        created = False
        prep_dir = None
        wav = wav_path
        try:
            if not wav:
                # 独立临时目录（#126 B1）：自建 wav 落 mkdtemp 而非源文件同目录——
                # 两个并发任务处理同一文件时互不覆盖，清理也互不误删
                prep_dir = tempfile.mkdtemp(prefix="vn_dia_")
                # 立即置位（#127 B4）：normalize_to_wav 抛错时 finally 也能清掉目录，
                # 不再泄漏 /tmp/vn_dia_XXXX
                created = True
                wav = normalize_to_wav(audio_file, out_dir=prep_dir)
            turns = diarize_audio(wav, num_speakers=mgr.get_diarization_speakers())
            segments = assign_speakers(segments, turns)
            speaker_count = len(
                {s.speaker for s in segments if getattr(s, "speaker", None)}
            )
            logger.info("说话人分离完成: %d 段、%d 位说话人", len(segments), speaker_count)
            return segments
        finally:
            if created and prep_dir:
                shutil.rmtree(prep_dir, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 —— diarization 失败不阻断笔记生成
        logger.warning("说话人分离失败（跳过）: %s", exc)
        return segments


def transcribe_audio(audio_file: Union[str, Path], transcriber: Optional[Transcriber] = None) -> dict:
    """只做语音识别：给定音频/视频文件 → 转写结果 asdict（{language, full_text, segments}）。

    不配置 transcriber 时按当前转写器配置构建。
    当 setup 启用「音频预处理」（enable_preprocess）时，先归一化为 16kHz mono wav，
    超长音频按块转写并拼接（时间偏移补偿）；默认关闭时行为与之前完全一致。
    配置启用说话人分离（diarization）时给每段打 speaker（docs/05 #31）。
    """
    audio_file = str(audio_file)
    if not Path(audio_file).exists():
        raise FileNotFoundError(f"音频/视频文件不存在: {audio_file}")
    if transcriber is None:
        transcriber = build_transcriber()

    if not _preprocess_enabled():
        tr = transcriber.transcript(file_path=audio_file)
        segments = apply_diarization(audio_file, list(tr.segments or []))
        _ensure_transcript_content(tr.full_text or "", list(tr.segments or []))
        return asdict(
            TranscriptResult(language=tr.language, full_text=tr.full_text, segments=segments)
        )

    # 预处理模式：归一 + 分块 → 逐块转写 + 时间偏移拼接
    return _transcribe_with_preprocess(audio_file, transcriber)


def _preprocess_enabled() -> bool:
    """读转写配置的 enable_preprocess（默认关）。"""
    try:
        from app.services.transcriber_config_manager import TranscriberConfigManager

        return TranscriberConfigManager().get_enable_preprocess()
    except Exception:
        return False


def _ensure_transcript_content(full_text: str, segments: list) -> None:
    """空转写（无文字且无分段）按失败处理，而不是成功（#121 B2）。

    whisper 对静音/黑屏/损坏音频常返回空：上层曾把空转写当成功缓存，
    任务 SUCCESS 后 LLM 拿空素材凭空生成笔记。两个分支（直转/预处理）统一检查。
    """
    if not (full_text or "").strip() and not segments:
        raise RuntimeError(
            "转写结果为空（无文字且无分段）：音频可能静音或损坏，或转写器未生效；"
            "请检查音视频文件或换转写引擎重试"
        )


def _transcribe_with_preprocess(audio_file: str, transcriber: Transcriber) -> dict:
    """预处理后逐块转写并拼接 segments（时间偏移补偿）。"""
    from app.models.transcriber_model import TranscriptSegment
    from app.transcriber.audio_preprocess import chunk_if_long, normalize_to_wav

    # 独立临时目录（#126 B1）：prep 产物（16k wav + 分块）不再落源文件同目录——
    # 旧实现固定命名 <名>_16k.wav，两个并发任务处理同一文件会互相覆盖/误删对方
    # 正在转写的 wav；mkdtemp 隔离后各任务只碰自己的目录。
    prep_dir = tempfile.mkdtemp(prefix="vn_prep_")
    try:
        wav = normalize_to_wav(audio_file, out_dir=prep_dir)
        chunks = chunk_if_long(wav, max_seconds=1800)

        all_segments: List[TranscriptSegment] = []
        offset = 0.0
        language = None
        failed = 0
        first_error: Optional[Exception] = None
        for chunk in chunks:
            chunk_dur = chunk_duration_guess(chunk)
            try:
                tr = transcriber.transcript(file_path=chunk)
            except Exception as exc:  # noqa: BLE001 —— 单块失败跳过，不阻断整段
                logger.warning(f"预处理分块转写失败（跳过该块）: {exc}")
                if first_error is None:
                    first_error = exc
                failed += 1
                # 失败也要推进时间偏移，否则后续段时间轴整体错位（缺了这块的 ~1800s）
                offset += chunk_dur
                continue
            if language is None and tr.language:
                language = tr.language
            for seg in tr.segments or []:
                all_segments.append(
                    TranscriptSegment(
                        start=round(seg.start + offset, 3),
                        end=round(seg.end + offset, 3),
                        text=seg.text,
                    )
                )
            offset += chunk_dur

        # 说话人分离：复用已归一化的 wav，在临时目录清理前完成（docs/05 #31）
        all_segments = apply_diarization(audio_file, all_segments, wav_path=wav)
        # 分块文本用空格连接：无分隔拼接会让英文 chunk 边界连词（"hello"+"hello" → "hellohello"）
        full_text = " ".join(s.text for s in all_segments)
        if failed == len(chunks):
            # 全块失败曾静默返回空转写，上层当成功缓存 → 任务 SUCCESS 产空笔记（#118）
            raise RuntimeError(
                f"预处理分块转写全部失败（{failed}/{len(chunks)} 块）：{first_error}"
            ) from first_error
        # 单块成功但内容为空（静音块）同样按失败处理（#121 B2）
        _ensure_transcript_content(full_text, list(all_segments))
        result = asdict(
            TranscriptResult(language=language, full_text=full_text, segments=all_segments)
        )
        if failed:
            logger.warning(f"预处理分块转写部分失败（{failed}/{len(chunks)} 块），转写不完整")
            result["truncated"] = True
        else:
            # 全成功：不输出 truncated 键（保持旧契约形状，B2 只在不完整时新增标记）
            result.pop("truncated", None)
        return result
    finally:
        shutil.rmtree(prep_dir, ignore_errors=True)


def chunk_duration_guess(wav_path: str) -> float:
    """估算分块时长（秒），用于时间偏移。用 ffprobe 精确值，失败回退块时长。"""
    try:
        from app.transcriber.audio_preprocess import probe_duration

        d = probe_duration(wav_path)
        if d > 0:
            return d
        logger.warning("probe_duration 返回非正值 %r，分块时长回退 1800s（时间轴可能漂移）: %s", d, wav_path)
    except Exception as exc:  # noqa: BLE001 —— 探测失败按默认分块时长兜底，但必须留痕
        logger.warning("probe_duration 失败，分块时长回退 1800s（时间轴可能漂移）: %s: %s", wav_path, exc)
    return 1800.0  # 兜底：等于默认分块时长


# ---------------- 步骤 3：视频关键帧抽取（画面理解素材） ----------------

def extract_frames(
    video_path: Union[str, Path],
    video_interval: int = 6,
    grid_size: Optional[List[int]] = None,
    save_dir: Optional[Union[str, Path]] = None,
) -> List[str]:
    """只做视频画面理解素材：给定本地 mp4 → 按间隔抽帧并持久化。

    返回持久化后的帧图片 **file:// 绝对路径** 列表（供多模态模型 Read / 喂给 summarize_material）。
    save_dir 缺省为 note_results/frames_<视频名>_<随机后缀>/：不同目录的同名视频
    并发处理不再互相覆盖帧文件，重复处理也不会混入旧帧（#124 B19）。
    """
    video_path = str(video_path)
    if not Path(video_path).exists():
        raise FileNotFoundError(f"视频文件不存在: {video_path}")
    grid = tuple(grid_size) if grid_size else (3, 3)
    reader = VideoReader(
        video_path=video_path,
        grid_size=grid,
        frame_interval=int(video_interval) or 6,
        unit_width=960,
        unit_height=540,
        save_quality=80,
    )
    data_uris = reader.run()

    if save_dir is None:
        save_dir = NOTE_OUTPUT_DIR / f"frames_{Path(video_path).stem}_{uuid4().hex[:8]}"
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    frames: List[str] = []
    for i, data_uri in enumerate(data_uris, start=1):
        try:
            if isinstance(data_uri, str) and data_uri.startswith("data:image"):
                b64 = data_uri.split(",", 1)[1]
                p = save_dir / f"frame_{i}.jpg"
                p.write_bytes(base64.b64decode(b64))
                frames.append(p.as_uri())
            else:
                logger.warning(f"跳过非 data URI 帧 (index={i}): {str(data_uri)[:60]}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"帧 {i} 落盘失败，跳过: {exc}")
    return frames


# ---------------- 步骤 4：弹幕 + 评论区观点 ----------------

def fetch_comments_danmaku(video_url: str, comments_limit: int = 20) -> Optional[str]:
    """抓取 B 站弹幕汇总 + 热门评论，拼成一段提示词文本（失败返回 None，不阻断）。

    与 fetch_comments / fetch_danmaku 两个独立工具同源（BilibiliCommentFetcher），
    这里是「拼接成一段」的聚合版，供 summarize_material / generate 直接注入。
    """
    parts: List[str] = []
    try:
        from app.downloaders.bilibili_comment import BilibiliCommentFetcher

        fetcher = BilibiliCommentFetcher()

        danmaku = fetcher.fetch_danmaku(str(video_url))
        if danmaku.get("ok"):
            summary = danmaku.get("danmaku_summary") or ""
            if summary:
                parts.append(f"【弹幕】\n{summary}")
        else:
            logger.warning(f"弹幕抓取失败，跳过: {danmaku.get('error')}")

        comments = fetcher.fetch_comments(str(video_url), limit=comments_limit)
        if comments.get("ok"):
            rows = comments.get("comments") or []
            if rows:
                lines = [
                    f"- {c.get('user', '')}({c.get('likes', 0)}赞): {c.get('content', '')}"
                    for c in rows
                ]
                parts.append("【热门评论】\n" + "\n".join(lines))
        else:
            logger.warning(f"评论抓取失败，跳过: {comments.get('error')}")
    except Exception as exc:  # noqa: BLE001 —— 任何网络/解析异常都不阻断任务
        logger.warning(f"弹幕/评论抓取失败，跳过: {exc}")
        return None

    if not parts:
        return None
    return "\n\n".join(parts)


# ---------------- 步骤 5：LLM 总结（吃素材包） ----------------

def _frames_to_data_uris(frames: Optional[List[str]]) -> List[str]:
    """把素材包里的帧转成 base64 data URI（GPTSource.video_img_urls 用）。

    兼容两种输入：已是 `data:image/...` 的 data URI（如 generate 内部的 video_img_urls）直接透传；
    `file://` 绝对路径则读文件转 base64。
    """
    if not frames:
        return []
    uris: List[str] = []
    for f in frames:
        try:
            s = str(f)
            if s.startswith("data:image"):
                uris.append(s)  # 已是 data URI，直接透传
                continue
            p = Path(s)
            if s.startswith("file://"):
                from urllib.parse import unquote, urlparse

                # 必须 unquote：as_uri() 会把空格/中文编码成 %20，不解码 exists() 恒 False
                p = Path(unquote(urlparse(s).path))
            if not p.exists():
                logger.warning(f"帧文件不存在，跳过: {f}")
                continue
            b64 = base64.b64encode(p.read_bytes()).decode("utf-8")
            uris.append(f"data:image/jpeg;base64,{b64}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"帧转 base64 失败，跳过: {f}: {exc}")
    return uris


def summarize_material(
    material: dict,
    gpt: GPT,
    style: Optional[str] = None,
    extras: Optional[str] = None,
    formats: Optional[List[str]] = None,
    screenshot: bool = False,
    link: bool = False,
    tags: Optional[List] = None,
    checkpoint_key: Optional[str] = None,
    cancel_event=None,
) -> str:
    """只做 LLM 总结：给定素材包（转写/帧/评论）+ GPT 实例 → 返回 Markdown。

    不写缓存、不写库、不更新状态 —— 那些是编排层（generate）的职责。
    素材包缺字段时安全兜底（title 空、无帧、无评论都可用）。tags 透传给 GPTSource
    （generate() 重构时传 audio_meta.raw_info.get("tags", []) 保持行为一致）。
    """
    transcript = material.get("transcript") or {}
    segments: List = transcript.get("segments") or []
    seg_objs: List[TranscriptSegment] = []
    for s in segments:
        if isinstance(s, TranscriptSegment):
            seg_objs.append(s)
            continue
        # 外部素材（fetch_subtitles / 用户自备 JSON）的段常带额外键（words/id/line_id）
        # 或缺字段——`TranscriptSegment(**s)` 会对未知键抛 TypeError，后台 FAILED 信息
        # 完全不可操作（#124 A9）：过滤到已知键，缺 start/end/text 的段跳过留痕
        if not isinstance(s, dict):
            logger.warning(f"跳过非对象的转写段: {str(s)[:40]!r}")
            continue
        known = {k: s.get(k) for k in ("start", "end", "text", "speaker") if k in s}
        if "start" not in known or "end" not in known or "text" not in known:
            logger.warning(f"跳过缺字段的转写段（需要 start/end/text）: {str(s)[:80]!r}")
            continue
        seg_objs.append(TranscriptSegment(**known))

    source = GPTSource(
        title=material.get("title") or "",
        segment=seg_objs,
        tags=tags or [],
        screenshot=screenshot,
        video_img_urls=_frames_to_data_uris(material.get("frames")),
        comments_danmaku=material.get("comments_danmaku"),
        link=link,
        _format=formats or [],
        style=style,
        extras=extras,
        checkpoint_key=checkpoint_key,
    )
    return gpt.summarize(source, cancel_event=cancel_event)
