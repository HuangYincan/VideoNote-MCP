import logging
import os
import tempfile
import threading
from abc import ABC
from typing import List, Optional, Union

import yt_dlp

from app.downloaders.base import Downloader, DownloadQuality
from app.downloaders.common import ytdlp_cancel_hook, ytdlp_retry
from app.downloaders.youtube_subtitle import YouTubeSubtitleFetcher
from app.models.notes_model import AudioDownloadResult
from app.models.transcriber_model import TranscriptResult
from app.services.cookie_manager import CookieConfigManager
from app.services.proxy_config_manager import ProxyConfigManager
from app.utils.path_helper import get_data_dir
from app.utils.url_parser import extract_video_id
from app.utils.url_safety import assert_public_http_url, sanitize_url

logger = logging.getLogger(__name__)


def _apply_proxy(ydl_opts: dict) -> dict:
    """YouTube 在国内需要代理。配置了全局代理就塞进 yt-dlp opts。"""
    proxy = ProxyConfigManager().get_proxy_url()
    if proxy:
        ydl_opts['proxy'] = proxy
        # 代理 URL 可能含 user:pass@（docs/05 第 16 轮 A4）：日志只留 host，不落凭据
        logger.info(f"yt-dlp 走代理: {sanitize_url(proxy)}")
    return ydl_opts


# 浏览器样请求头（2026-08-19）：YouTube 对非浏览器 UA 的请求更容易触发人机验证/反爬，
# 显式用 Chrome UA + 常规 Accept/Accept-Language 降低触发概率。UA 可用
# VIDEONOTE_YTDLP_UA 环境变量覆盖（如换 Firefox 或其他版本 Chrome）。
_DEFAULT_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _apply_browser_headers(ydl_opts: dict) -> dict:
    """给 yt-dlp 请求套浏览器样 headers，降低 YouTube 人机验证/反爬触发概率。"""
    ua = os.environ.get("VIDEONOTE_YTDLP_UA") or _DEFAULT_HTTP_HEADERS["User-Agent"]
    headers = dict(_DEFAULT_HTTP_HEADERS)
    headers["User-Agent"] = ua
    # yt-dlp 已有 http_headers 时（如 extractor 注入）合并而不是覆盖
    ydl_opts['http_headers'] = {**ydl_opts.get('http_headers', {}), **headers}
    return ydl_opts


def _apply_js_challenge(ydl_opts: dict) -> dict:
    """YouTube 2026+ 的 JS challenge（签名/n 求解）硬性要求（2026-08-19 实测）：

    - EJS 远程组件：yt-dlp 默认**不下载** challenge solver 脚本，须显式允许
      `ejs:github`，否则报「The page needs to be reloaded」；
    - node runtime：自动检测在 uvx 隔离环境下不可靠（node 在 PATH 也找不到），
      显式指定 `{'node': {}}` 才生效。
    缺任一条件，即使有登录 cookie + 代理也会失败。
    """
    ydl_opts['remote_components'] = ['ejs:github']
    ydl_opts['js_runtimes'] = {'node': {}}
    return ydl_opts


def cookie_string_to_netscape(cookie: str) -> Optional[str]:
    """Cookie 字符串（name=value; ...）→ Netscape 格式临时文件路径（yt-dlp cookiefile 用）。

    CLI `videonote cookie set` / `login youtube` 的验证与下载器共用同一写入逻辑。
    """
    if not cookie:
        return None
    lines = ["# Netscape HTTP Cookie File\n"]
    for pair in cookie.split("; "):
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
    return tmp.name


class YoutubeDownloader(Downloader, ABC):
    def __init__(self):
        super().__init__()
        # YouTube Cookie（docs/05 #34）：经 setup ③「平台 Cookie」填 youtube，
        # 或 `videonote login youtube --browser safari` 直接读浏览器登录态；
        # 高清/年龄限制/地区限制视频需要登录态，匿名时照常降级
        self._cookie_mgr = CookieConfigManager()
        self._cookie = self._cookie_mgr.get('youtube')
        self._browser = self._cookie_mgr.get_browser('youtube')
        self._cookiefile = cookie_string_to_netscape(self._cookie)

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
        cancel_event: Optional[threading.Event] = None,
    ) -> AudioDownloadResult:
        # SSRF 防护（docs/05 第 16 轮 A1）：YouTube URL 也经 yt-dlp 抓取
        assert_public_http_url(video_url)
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
            'progress_hooks': [ytdlp_cancel_hook(cancel_event)],
        }
        if self._browser:
            # cookiesfrombrowser 优先：直接读浏览器登录态（`videonote cookie from-browser`），
            # 无需手动导出 cookie 字符串；读取失败由 yt-dlp 报错并降级
            ydl_opts['cookiesfrombrowser'] = (self._browser,)
        elif self._cookiefile:
            ydl_opts['cookiefile'] = self._cookiefile

        if skip_download:
            ydl_opts['skip_download'] = True

        _apply_proxy(ydl_opts)
        _apply_browser_headers(ydl_opts)
        _apply_js_challenge(ydl_opts)
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
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        """
        下载视频，返回视频文件路径
        """
        # SSRF 防护（docs/05 第 16 轮 A1）
        assert_public_http_url(video_url)
        if output_dir is None:
            output_dir = get_data_dir()
        video_id = extract_video_id(video_url, "youtube")
        if not video_id:
            raise ValueError(f"无法从链接提取 YouTube 视频 ID: {video_url}")
        video_path = os.path.join(output_dir, f"{video_id}.mp4")
        if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
            return video_path
        # 0 字节/半截残留（上次中断，docs/05 第 16 轮 B11）：删掉重下，
        # 否则抽帧拿到损坏视频报泛化错误；kuaishou mp3 已有同款守卫（#124 B1）
        if os.path.exists(video_path):
            try:
                os.unlink(video_path)
            except OSError:
                pass
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, "%(id)s.%(ext)s")

        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
            'outtmpl': output_path,
            'noplaylist': True,
            'quiet': False,
            'merge_output_format': 'mp4',  # 确保合并成 mp4
            'progress_hooks': [ytdlp_cancel_hook(cancel_event)],
        }
        if self._browser:
            # cookiesfrombrowser 优先：直接读浏览器登录态（`videonote cookie from-browser`），
            # 无需手动导出 cookie 字符串；读取失败由 yt-dlp 报错并降级
            ydl_opts['cookiesfrombrowser'] = (self._browser,)
        elif self._cookiefile:
            ydl_opts['cookiefile'] = self._cookiefile

        _apply_proxy(ydl_opts)
        _apply_browser_headers(ydl_opts)
        _apply_js_challenge(ydl_opts)
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
