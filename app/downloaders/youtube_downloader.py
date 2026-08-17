import logging
import os
import tempfile
from abc import ABC
from typing import List, Optional, Union

import yt_dlp

from app.downloaders.base import Downloader, DownloadQuality
from app.downloaders.common import ytdlp_retry
from app.downloaders.youtube_subtitle import YouTubeSubtitleFetcher
from app.models.notes_model import AudioDownloadResult
from app.models.transcriber_model import TranscriptResult
from app.services.cookie_manager import CookieConfigManager
from app.services.proxy_config_manager import ProxyConfigManager
from app.utils.path_helper import get_data_dir
from app.utils.url_parser import extract_video_id

logger = logging.getLogger(__name__)


def _apply_proxy(ydl_opts: dict) -> dict:
    """YouTube 在国内需要代理。配置了全局代理就塞进 yt-dlp opts。"""
    proxy = ProxyConfigManager().get_proxy_url()
    if proxy:
        ydl_opts['proxy'] = proxy
        logger.info(f"yt-dlp 走代理: {proxy}")
    return ydl_opts


class YoutubeDownloader(Downloader, ABC):
    def __init__(self):
        super().__init__()
        # YouTube Cookie（docs/05 #34）：经 setup ③「平台 Cookie」填 youtube；
        # 高清/年龄限制/地区限制视频需要登录态，匿名时照常降级
        self._cookie_mgr = CookieConfigManager()
        self._cookie = self._cookie_mgr.get('youtube')
        self._cookiefile = self._write_netscape_cookie_file()

    def _write_netscape_cookie_file(self) -> Optional[str]:
        """将 Cookie 写入 Netscape 格式临时文件，返回文件路径（供 yt-dlp cookiefile 使用）。"""
        if not self._cookie:
            return None
        lines = ["# Netscape HTTP Cookie File\n"]
        for pair in self._cookie.split("; "):
            if "=" in pair:
                key, value = pair.split("=", 1)
                lines.append(f".youtube.com\tTRUE\t/\tFALSE\t0\t{key}\t{value}\n")
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8')
        tmp.writelines(lines)
        tmp.close()
        try:
            os.chmod(tmp.name, 0o600)
        except OSError:
            pass
        logger.info("已生成 YouTube Netscape Cookie 文件（条目: %d）", len(lines) - 1)
        return tmp.name

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
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'outtmpl': output_path,
            'noplaylist': True,
            'quiet': False,
        }
        if self._cookiefile:
            ydl_opts['cookiefile'] = self._cookiefile

        if skip_download:
            ydl_opts['skip_download'] = True

        _apply_proxy(ydl_opts)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ytdlp_retry(ydl.extract_info, video_url, download=not skip_download)
            video_id = info.get("id")
            title = info.get("title")
            duration = info.get("duration", 0)
            cover_url = info.get("thumbnail")
            ext = info.get("ext", "m4a")
            audio_path = os.path.join(output_dir, f"{video_id}.{ext}")

        return AudioDownloadResult(
            file_path=audio_path,
            title=title,
            duration=duration,
            cover_url=cover_url,
            platform="youtube",
            video_id=video_id,
            raw_info={'tags': info.get('tags')},
            video_path=None,
        )

    def download_video(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
    ) -> str:
        """
        下载视频，返回视频文件路径
        """
        if output_dir is None:
            output_dir = get_data_dir()
        video_id = extract_video_id(video_url, "youtube")
        if not video_id:
            raise ValueError(f"无法从链接提取 YouTube 视频 ID: {video_url}")
        video_path = os.path.join(output_dir, f"{video_id}.mp4")
        if os.path.exists(video_path):
            return video_path
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, "%(id)s.%(ext)s")

        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
            'outtmpl': output_path,
            'noplaylist': True,
            'quiet': False,
            'merge_output_format': 'mp4',  # 确保合并成 mp4
        }
        if self._cookiefile:
            ydl_opts['cookiefile'] = self._cookiefile

        _apply_proxy(ydl_opts)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ytdlp_retry(ydl.extract_info, video_url, download=True)
            video_id = info.get("id")
            video_path = os.path.join(output_dir, f"{video_id}.mp4")

        if not os.path.exists(video_path):
            raise FileNotFoundError(f"视频文件未找到: {video_path}")

        return video_path

    def download_subtitles(self, video_url: str, output_dir: str = None,
                           langs: List[str] = None) -> Optional[TranscriptResult]:
        """
        通过 YouTube InnerTube API 直接获取字幕（优先人工字幕，其次自动生成）。
        比 yt_dlp 方式更轻量，无需写临时文件到磁盘。

        :param video_url: 视频链接
        :param output_dir: 未使用（保留接口兼容）
        :param langs: 优先语言列表
        :return: TranscriptResult 或 None
        """
        if langs is None:
            langs = ['zh-Hans', 'zh', 'zh-CN', 'zh-TW', 'en', 'en-US', 'ja']

        video_id = extract_video_id(video_url, "youtube")
        fetcher = YouTubeSubtitleFetcher()
        logger.info("尝试获取字幕，video_id=%s, langs=%s", video_id, langs)
        try:
            return fetcher.fetch_subtitles(video_id, langs)
        finally:
            # 显式释放代理 Session（#125 B16 定义了 close 但唯一生产调用路径
            # 直接 return 没调——MCP 长驻进程 GC 不保证及时，连接池泄漏仍在，#126 B2）
            fetcher.close()
