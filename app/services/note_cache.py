"""跨任务内容缓存：同一视频（platform:video_id）复用上次的转写，避免重下/重转写。

现状：每次 `generate_note` / `prepare_note_material` 都新开 `uuid4` 任务文件夹，
`gen/transcript.json` 缓存只对**同一 task_id 重跑**有效；同一视频再次生成 =
完整重下载 + 重转写（转写是整条流水线最贵的部分）。

命中路径刻意做成「最省事」：命中 → 把缓存里的 transcript.json 拷进
`{task_id}/gen/transcript.json`。下游已有「读到 gen/transcript.json 就跳过字幕与
音频转写」的逻辑（has_transcript → `_download_media(skip_download=True)`，只做元信息
提取、不下载媒体），所以只需 pre-populate，不改下载 / 转写路径。同时把**上次下载的
音频媒体**也收进缓存（`<ident>/media/`），命中时复制到新任务 raw/ —— 让 material 的
`audio_path` 指向真实文件，不悬空。

身份键：`platform:video_id`，video_id 从 URL 预解析（B 站 BV+p / YouTube v= / 抖音 /
TikTok；本地文件用 sha256）。b23 短链解析失败、快手、generic 等解析不出 → 不命中
（保持原行为）。转写完成后按下载器权威 `audio_meta.video_id` promote（bilibili 归一化
`BV..._pN` 后缀）。

转写按来源分键：
  - `subtitle`：平台字幕（引擎无关，质量最高）；
  - 本地/云端引擎：`transcript_{transcriber_type}[:{model_size}]`——本地引擎拼
    model_size，切换引擎或模型尺寸不会误用旧结果。

淘汰：无 LRU。`cleanup_all` 连缓存一起清。
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

from app.utils.url_parser import extract_bilibili_p_number, extract_video_id

logger = logging.getLogger(__name__)

# lookup_media 认可的媒体后缀（#123 B2）：.tmp 与不在白名单的后缀视为非媒体，跳过
_MEDIA_SUFFIXES = {
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus",
    ".mp4", ".webm", ".mkv", ".mov",
}

# 平台字幕转写的缓存分键（引擎无关）
SUBTITLE_KEY = "subtitle"

# 本地推理引擎：转写结果依赖模型尺寸，分键时要拼 model_size
_LOCAL_ENGINES = {"fast-whisper", "whisper", "mlx-whisper", "funasr"}


def _fs_safe(s: str) -> str:
    """Windows 文件名安全化：冒号/斜杠等非法字符替换为 '-'。

    身份键（platform:video_id）与引擎分键可能含冒号，Windows 上 mkdir/写入会抛
    OSError 被吞掉，缓存静默永不命中（docs/05 #60）。改键后旧缓存（冒号形式）
    miss 一次重新转写，可接受。
    """
    return re.sub(r"[:/\\<>*?|]", "-", s)


def cache_root() -> Path:
    """跨任务缓存根：<note_results 的父目录>/note_cache。

    与 note_results 平级：per-task 清理（cleanup_note）以任务文件夹为边界，
    不会误删缓存；以 NOTE_OUTPUT_DIR.parent 为准，与测试隔离一致。
    """
    from app.services.note import NOTE_OUTPUT_DIR  # 惰性：note.py 顶层 import 本模块

    return NOTE_OUTPUT_DIR.parent / "note_cache"


def engine_key(transcriber_type: str, model_size: str) -> str:
    """转写来源分键。本地引擎拼 model_size（tiny/small/base…），云端引擎只按类型。"""
    size = (model_size or "").strip()
    if transcriber_type in _LOCAL_ENGINES and size:
        return _fs_safe(f"{transcriber_type}:{size}")
    return transcriber_type


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@functools.lru_cache(maxsize=64)
def _sha256_cached(path: str, mtime_ns: int, size: int) -> str:
    """按 (路径, mtime, size) 缓存文件哈希——文件更新自动失效。

    #123 B4：本地文件身份每任务要算 2-3 次（lookup_transcript / lookup_media /
    promote 各自 derive_video_id），全量 sha256 对大视频是毫秒-秒级重复开销。
    同任务多次调用共享一次哈希；文件被修改（mtime/size 变化）→ 重新计算，正确性保持。
    """
    return _sha256_file(Path(path))


def _has_segments(path: Path) -> bool:
    """转写文件是否含至少 1 段（0 段 = 静音/无语音，无信息量，不缓存也不命中）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return bool(data.get("segments"))
    except (OSError, ValueError):
        return False


def derive_video_id(url: str, platform: str) -> Optional[str]:
    """从 URL 预解析稳定视频 id（免下载优先）。解析不出返回 None（不命中缓存）。

    - bilibili：BV + p（`BV...:pN`，无 p 就是 `BV...`），分 P 之间身份互不污染；
    - youtube / douyin / tiktok / xiaoyuzhou：URL 内 id；
    - local：文件 sha256（内容变了就换身份，正确性优先）；
    - 其余（kuaishou / generic 等）：None。
    """
    if platform == "local":
        try:
            st = Path(url).stat()
            # (path, mtime_ns, size) 缓存键：同任务多次调用共享一次哈希（#123 B4）
            return _sha256_cached(str(Path(url)), st.st_mtime_ns, st.st_size)
        except OSError:
            return None
    if platform == "bilibili":
        # b23.tv 只解一次短链：extract_video_id 与 extract_bilibili_p_number 各自
        # 解一次 = 每次 lookup 两次 HEAD（各 timeout 5-10s，慢网 30s，#125 B6）。
        # 先解，BV 与 p 都从真实 URL 提取（短链自身不含 ?p=）。
        if "b23.tv" in url:
            from app.utils.url_parser import resolve_bilibili_short_url

            url = resolve_bilibili_short_url(url) or url
        bvid = extract_video_id(url, "bilibili")
        if not bvid:
            return None
        p = extract_bilibili_p_number(url)
        return f"{bvid}:p{p}" if p else bvid
    vid = extract_video_id(url, platform)  # youtube / douyin
    if vid is None and platform == "tiktok":
        m = re.search(r"/video/(\d+)", url)
        vid = m.group(1) if m else None
    return vid


def _normalize_bili_video_id(video_id: str) -> str:
    """把下载器返回的 video_id（BV… / BV…_pN / BV…pN）归一到缓存身份（BV[:pN]）。"""
    m = re.match(r"(BV[0-9A-Za-z]+)(?:_?p(\d+))?", str(video_id or ""))
    if not m:
        return str(video_id)
    p = int(m.group(2)) if m.group(2) else None
    return f"{m.group(1)}:p{p}" if p else m.group(1)


def identity_for(url: str, platform: str, audio_video_id: Optional[str] = None) -> Optional[str]:
    """缓存身份键（`platform:video_id`）。

    查找用 URL 解析（audio_video_id 未知）；promote 时优先 URL 解析（与查找一致，
    bilibili 含 p），URL 解析不出才用下载器权威 video_id 兜底（如 b23 短链）。
    """
    if platform == "bilibili":
        url_id = derive_video_id(url, "bilibili")
        if url_id:
            return _fs_safe(f"bilibili:{url_id}")
        if audio_video_id:
            return _fs_safe(f"bilibili:{_normalize_bili_video_id(audio_video_id)}")
        return None
    vid = derive_video_id(url, platform)
    if vid:
        return _fs_safe(f"{platform}:{vid}")
    if audio_video_id and platform != "local":
        return _fs_safe(f"{platform}:{audio_video_id}")
    return None


def lookup_transcript(
    url: str,
    platform: str,
    transcriber_type: str,
    model_size: str,
    dest: Path,
) -> Optional[Path]:
    """查找内容缓存；命中则把缓存 transcript 拷到 dest（任务 gen/transcript.json）。

    查找顺序：当前引擎键（精确 memo，切换引擎/尺寸不误用）→ subtitle 键（平台字幕，
    引擎无关）。解析不出身份 / 读取失败 → 返回 None（调用方走原流程）。
    """
    ident = identity_for(url, platform)
    if not ident:
        return None
    base = cache_root() / ident
    for key in (engine_key(transcriber_type, model_size), SUBTITLE_KEY):
        src = base / f"transcript_{key}.json"
        if not src.exists():
            continue
        if not _has_segments(src):
            # 历史遗留的空转写（静音视频）：当 miss 处理，继续找下一个分键
            logger.info("跳过空转写缓存 %s", src)
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            logger.info("命中跨任务转写缓存: %s", src)
            return src
        except OSError as exc:
            logger.warning("读跨任务转写缓存失败 %s: %s", src, exc)
            return None
    return None


def promote_transcript(
    platform: str,
    url: str,
    audio_video_id: Optional[str],
    engine: Optional[str],
    src: Path,
) -> None:
    """把任务刚产出的 transcript.json 拷进跨任务缓存（尽力而为，失败只记日志）。

    engine 由调用方按转写来源传入：SUBTITLE_KEY 或 engine_key(...)。无引擎
    （如本次转写来自 per-task 缓存）或 src 不存在时不动作。
    """
    if not engine or not src.exists():
        return
    if not _has_segments(src):
        # 空转写不进缓存：静音/无语音视频的 0 段结果会被下次任务短路且无信息量
        logger.info("转写为空（0 段），不写入跨任务缓存")
        return
    ident = identity_for(url, platform, audio_video_id)
    if not ident:
        return
    try:
        base = cache_root() / ident
        base.mkdir(parents=True, exist_ok=True)
        dst = base / f"transcript_{engine}.json"
        # tmp 带唯一后缀（docs/05 第 16 轮 B3）：两个并发任务 promote 同一 ident 时
        # 不再共用 <dst>.tmp 互相截断——各自写完整内容，最后一次 replace 赢且恒为整文件
        tmp = dst.with_suffix(f".{uuid.uuid4().hex}.tmp")
        shutil.copyfile(src, tmp)
        tmp.replace(dst)  # 原子替换，避免读到半截文件
        logger.info("写入跨任务转写缓存: %s", dst)
    except OSError as exc:
        logger.warning("写跨任务转写缓存失败 %s: %s", ident, exc)


def promote_media(
    platform: str,
    url: str,
    audio_video_id: Optional[str],
    src_path: Optional[str],
) -> None:
    """把下载的音频媒体复制进缓存（`note_cache/<ident>/media/`，引擎无关）。

    供命中缓存的任务复制出真实音频（audio_path 不悬空）。local 跳过：源文件在用户磁盘，
    永久存在。src 不存在（字幕路径没下载媒体）或复制失败 → 只记日志。
    """
    if platform == "local" or not src_path:
        return
    src = Path(src_path)
    if not src.exists():
        return
    # 抖音/快手/generic 的「音频」实际是完整 mp4（数百 MB）：超过阈值只缓存转写，
    # 避免磁盘无界增长（docs/05 #59）
    MAX_MEDIA_CACHE_MB = 100
    try:
        if src.stat().st_size > MAX_MEDIA_CACHE_MB * 1024 * 1024:
            logger.info(
                "媒体超过 %dMB 不缓存（只缓存转写）: %s (%.1fMB)",
                MAX_MEDIA_CACHE_MB, src.name, src.stat().st_size / 1024 / 1024,
            )
            return
    except OSError:
        return
    ident = identity_for(url, platform, audio_video_id)
    if not ident:
        return
    try:
        base = cache_root() / ident / "media"
        base.mkdir(parents=True, exist_ok=True)
        dst = base / src.name
        # tmp 带唯一后缀（docs/05 第 16 轮 B3）：并发 promote 不共用 tmp，见 promote_transcript
        tmp = dst.with_suffix(f".{uuid.uuid4().hex}.tmp")
        shutil.copy2(src, tmp)
        tmp.replace(dst)  # 原子替换，避免读到半截文件
        logger.info("写入跨任务媒体缓存: %s", dst)
    except OSError as exc:
        logger.warning("写跨任务媒体缓存失败 %s: %s", ident, exc)


def lookup_media(url: str, platform: str, dest_dir: Path) -> Optional[str]:
    """命中媒体缓存 → 把音频复制到 dest_dir（新任务 raw/），返回新路径。

    miss（身份解析不出 / 无媒体缓存 / 复制失败）→ None（调用方维持原行为）。
    """
    ident = identity_for(url, platform)
    if not ident:
        return None
    src_dir = cache_root() / ident / "media"
    if not src_dir.is_dir():
        return None
    try:
        # 过滤 .tmp 半成品（promote_media 的 tmp→replace 之间进程被杀会遗留 <name>.tmp）
        # 与非常见媒体后缀——此前 iterdir 全选，.tmp 会被当音频复制给下游（#123 B2）。
        files = sorted(
            p for p in src_dir.iterdir()
            if p.is_file() and not p.name.endswith(".tmp")
            and p.suffix.lower() in _MEDIA_SUFFIXES
        )
    except OSError:
        return None
    if not files:
        return None
    try:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dst = dest_dir / files[0].name
        shutil.copy2(files[0], dst)
        logger.info("命中跨任务媒体缓存，复制到 %s", dst)
        return str(dst)
    except OSError as exc:
        logger.warning("复制跨任务媒体缓存失败: %s", exc)
        return None
