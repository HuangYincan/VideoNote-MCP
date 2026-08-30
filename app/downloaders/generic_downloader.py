"""通用平台下载器 —— 用 yt-dlp 默认提取器覆盖内置平台之外的站点。

yt-dlp 内置 1800+ 站点的提取器，且对未知 URL 有 GenericIE 兜底（自动嗅探
og:video / video 标签 / m3u8 等）。本类不指定 `ie_key`，让 yt-dlp 自动匹配：
内置站点走专属提取器，未知站点走 GenericIE —— 一次覆盖几乎所有平台。

对应 `detect_platform` 返回 `"generic"` 的平台（见 pipeline.py）。若 yt-dlp 也
解析失败（登录墙 / JS 难题），上游 server 层会回退到 `handoff_result` 让 Agent 接手。
"""
import logging
import os
import threading
from abc import ABC
from typing import Optional, Union

import yt_dlp

from app.downloaders.base import Downloader, DownloadQuality
from app.downloaders.common import ytdlp_cancel_hook, ytdlp_retry
from app.downloaders.youtube_downloader import _apply_browser_headers, _apply_proxy
from app.models.notes_model import AudioDownloadResult
from app.services.cookie_manager import CookieConfigManager
from app.utils.path_helper import get_data_dir
from app.utils.url_safety import assert_public_http_url

logger = logging.getLogger(__name__)


class GenericDownloader(Downloader, ABC):
    """yt-dlp 通用提取器下载器。"""

    def __init__(self):
        super().__init__()
        self._cookie_mgr = CookieConfigManager()
        self._cookie = None  # None=未取，""=已取但无配置

    def _get_cookie(self) -> str:
        """取 setup ③ 填的 generic 槽位 cookie。

        generic 站点无统一域名可预知，Netscape 文件会把 cookie 绑死在
        example.com 使 yt-dlp 永远不带 —— 改用 http_headers 的 Cookie 头
        直接注入，对目标站点及其 CDN 分片请求统一生效。
        """
        if self._cookie is None:
            self._cookie = self._cookie_mgr.get("generic") or ""
        return self._cookie

    def download(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
        quality: DownloadQuality = "fast",
        need_video: Optional[bool] = False,
        skip_download: bool = False,
        cancel_event: Optional[threading.Event] = None,
    ) -> AudioDownloadResult:
        # SSRF 防护（docs/05 第 16 轮 A1）：generic 是任意 URL 进入 yt-dlp 的入口
        assert_public_http_url(video_url)
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
            "progress_hooks": [ytdlp_cancel_hook(cancel_event)],
        }
        if skip_download:
            ydl_opts["skip_download"] = True
        cookie = self._get_cookie()
        if cookie:
            ydl_opts["http_headers"] = {"Cookie": cookie}
        _apply_proxy(ydl_opts)
        _apply_browser_headers(ydl_opts)

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

    def download_video(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        """通用视频下载：尽量合并 mp4。generic 场景主要用于音频，视频下载尽力而为。"""
        assert_public_http_url(video_url)
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
            "progress_hooks": [ytdlp_cancel_hook(cancel_event)],
        }
        cookie = self._get_cookie()
        if cookie:
            ydl_opts["http_headers"] = {"Cookie": cookie}
        _apply_proxy(ydl_opts)
        _apply_browser_headers(ydl_opts)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ytdlp_retry(ydl.extract_info, video_url, download=True)
        output_path = os.path.join(output_dir, f"{info.get('id')}.mp4")
        if not os.path.exists(output_path):
            raise RuntimeError(f"下载完成但未找到视频文件: {output_path}")
        return output_path
