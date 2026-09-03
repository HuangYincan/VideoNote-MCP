from dataclasses import asdict, dataclass
from typing import Optional

_SAFE_RAW_INFO_FIELDS = ("tags", "extractor", "ext")


@dataclass
class AudioDownloadResult:
    file_path: str               # 本地音频路径
    title: str                   # 视频标题
    duration: float              # 视频时长（秒）
    cover_url: Optional[str]     # 视频封面图
    platform: str                # 平台，如 "bilibili"
    video_id: str                # 唯一视频ID
    raw_info: dict               # 流水线内部元数据（不得直接返回 MCP）
    video_path: Optional[str] = None  #  新增字段：可选视频文件路径


def safe_audio_download_result_dict(result: AudioDownloadResult) -> dict:
    """Serialize audio metadata for the on-disk pipeline cache.

    Keep the small subset consumed by the note pipeline, rather than persisting
    the full yt-dlp info dictionary, which can contain signed URLs and headers.
    Cover URLs from Douyin/Kuaishou/Xiaohongshu almost always carry CDN tokens;
    strip the query so ``gen/audio.json`` cannot leak them into Agent Read.
    """
    from app.utils.url_safety import sanitize_url

    data = asdict(result)
    cover_url = data.get("cover_url")
    if isinstance(cover_url, str) and cover_url:
        data["cover_url"] = sanitize_url(cover_url) or None
    raw_info = data.get("raw_info")
    if isinstance(raw_info, dict):
        safe_raw_info = {}
        for key in _SAFE_RAW_INFO_FIELDS:
            if key not in raw_info:
                continue
            value = raw_info[key]
            if key == "tags":
                safe_raw_info[key] = [tag for tag in (value or []) if isinstance(tag, str)]
            elif isinstance(value, (str, int, float, bool)) or value is None:
                safe_raw_info[key] = value
        data["raw_info"] = safe_raw_info
    return data

