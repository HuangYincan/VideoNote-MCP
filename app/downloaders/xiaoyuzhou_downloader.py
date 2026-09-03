"""小宇宙下载器：音频走 yt-dlp，文稿走官方 transcript API。

未配置登录态时 download_subtitles 返回 None，流水线回退本地下载 + ASR
（47 分钟单集在 small 模型上会非常慢）。配好 token 后优先官方文稿，跳过转写。
"""
from __future__ import annotations

import threading
from typing import Optional, Union

from app.downloaders.generic_downloader import GenericDownloader
from app.downloaders.xiaoyuzhou_subtitle import XiaoyuzhouTranscriptFetcher
from app.enmus.note_enums import DownloadQuality
from app.exceptions.task import (
    OfficialTranscriptFetchError,
    TaskCancelledError,
    check_cancel,
)
from app.models.notes_model import AudioDownloadResult
from app.models.transcriber_model import TranscriptResult
from app.services.cookie_manager import CookieConfigManager
from app.utils.logger import get_logger
from app.utils.url_parser import extract_video_id

logger = get_logger(__name__)


class XiaoyuzhouDownloader(GenericDownloader):
    """yt-dlp 下音频 + 官方文稿优先。"""

    def _get_cookie(self) -> str:
        # 小宇宙槽位存的是 x-jike-* token，不是浏览器 Cookie；不要当 Cookie 头注入 yt-dlp。
        return ""

    def download(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
        quality: DownloadQuality = "fast",
        need_video: Optional[bool] = False,
        skip_download: bool = False,
        cancel_event: Optional[threading.Event] = None,
    ) -> AudioDownloadResult:
        result = super().download(
            video_url,
            output_dir=output_dir,
            quality=quality,
            need_video=need_video,
            skip_download=skip_download,
            cancel_event=cancel_event,
        )
        result.platform = "xiaoyuzhou"
        eid = extract_video_id(video_url, "xiaoyuzhou")
        if eid:
            result.video_id = eid
        return result

    def download_subtitles(
        self,
        video_url: str,
        output_dir: str = None,
        langs: list = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Optional[TranscriptResult]:
        check_cancel(cancel_event)
        if not CookieConfigManager().get("xiaoyuzhou"):
            logger.info(
                "未配置小宇宙登录态：官方文稿拿不到，将走语音识别。"
                "配置请走 CLI：`! videonote login xiaoyuzhou` 或 "
                "`videonote cookie set xiaoyuzhou '...'`；MCP 工具不收 token（安全红线）"
            )
        try:
            result = XiaoyuzhouTranscriptFetcher().fetch_subtitles(
                video_url, cancel_event=cancel_event
            )
            check_cancel(cancel_event)
            if result and result.segments:
                return result
        except TaskCancelledError:
            raise
        except OfficialTranscriptFetchError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("小宇宙官方文稿异常，将回退语音识别: %s", exc)
        return None
