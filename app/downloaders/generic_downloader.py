"""通用平台下载器 —— 用 yt-dlp 默认提取器覆盖内置 5 平台之外的站点。

yt-dlp 内置 1800+ 站点的提取器，且对未知 URL 有 GenericIE 兜底（自动嗅探
og:video / video 标签 / m3u8 等）。本类不指定 `ie_key`，让 yt-dlp 自动匹配：
内置站点走专属提取器，未知站点走 GenericIE —— 一次覆盖几乎所有平台。

对应 `detect_platform` 返回 `"generic"` 的平台（见 pipeline.py）。若 yt-dlp 也
解析失败（登录墙 / JS 难题），上游 server 层会回退到 `handoff_result` 让 Agent 接手。
"""
import os
import logging
from abc import ABC
from typing import Union, Optional

import yt_dlp

from app.downloaders.base import Downloader, DownloadQuality
from app.downloaders.common import ytdlp_retry
from app.downloaders.youtube_downloader import _apply_proxy
from app.models.notes_model import AudioDownloadResult
from app.services.cookie_manager import CookieConfigManager
from app.utils.path_helper import get_data_dir

logger = logging.getLogger(__name__)


class GenericDownloader(Downloader, ABC):
    """yt-dlp 通用提取器下载器。"""

    def __init__(self):
        super().__init__()
        self._cookie_mgr = CookieConfigManager()
        self._cookiefile = None

    def _ensure_cookie(self) -> None:
        """若配置过平台 Cookie，写成 Netscape cookiefile 供 yt-dlp 使用。

        跨平台 cookie 无统一槽位，仅当 detect_platform 识别出具体平台时可用；
        generic 场景多数站点无需登录，这里保持惰性（首次下载才写文件）。
        """
        if self._cookiefile is not None:
            return
        cookie = self._cookie_mgr.get("generic") or ""
        if not cookie:
            self._cookiefile = ""
            return
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(
                "# Netscape HTTP Cookie File\n"
                f"example.com\tTRUE\t/\tTRUE\t0\tgeneric\t{cookie}\n"
            )
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self._cookiefile = path

    def _cleanup_cookie_file(self) -> None:
        path = getattr(self, "_cookiefile", None)
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._cookiefile = None

    def __del__(self):
        self._cleanup_cookie_file()

    def download(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
        quality: DownloadQuality = "fast",
        need_video: Optional[bool] = False,
        skip_download: bool = False,
    ) -> AudioDownloadResult:
        if output_dir is None:
            output_dir = get_data_dir()
        if not output_dir:
            output_dir = self.cache_data
        os.makedirs(output_dir, exist_ok=True)

        output_path = os.path.join(output_dir, "%(id)s.%(ext)s")
        ydl_opts = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": output_path,
            "noplaylist": True,
            "quiet": False,
        }
        if skip_download:
            ydl_opts["skip_download"] = True
        self._ensure_cookie()
        if self._cookiefile:
            ydl_opts["cookiefile"] = self._cookiefile
        _apply_proxy(ydl_opts)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ytdlp_retry(ydl.extract_info, video_url, download=not skip_download)
                video_id = info.get("id")
                title = info.get("title")
                duration = info.get("duration", 0)
                cover_url = info.get("thumbnail")
                ext = info.get("ext", "m4a")
                audio_path = os.path.join(output_dir, f"{video_id}.{ext}")
        except Exception as exc:  # noqa: BLE001 —— yt-dlp 提取失败，交给上层 handoff
            logger.warning(f"generic 下载失败: {exc}")
            raise ValueError(f"无法用 yt-dlp 解析该链接（可能需登录/JS 渲染）: {exc}")

        return AudioDownloadResult(
            file_path=audio_path,
            title=title,
            duration=duration,
            cover_url=cover_url,
            platform="generic",
            video_id=video_id,
            raw_info={"extractor": info.get("extractor"), "ext": ext},
            video_path=None,
        )

    def download_video(self, video_url: str, output_dir: Union[str, None] = None) -> str:
        """通用视频下载：尽量合并 mp4。generic 场景主要用于音频，视频下载尽力而为。"""
        if output_dir is None:
            output_dir = get_data_dir()
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, "%(id)s.%(ext)s")
        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
            "outtmpl": output_path,
            "noplaylist": True,
            "quiet": False,
            "merge_output_format": "mp4",
        }
        self._ensure_cookie()
        if self._cookiefile:
            ydl_opts["cookiefile"] = self._cookiefile
        _apply_proxy(ydl_opts)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ytdlp_retry(ydl.extract_info, video_url, download=True)
        return os.path.join(output_dir, f"{info.get('id')}.mp4")
