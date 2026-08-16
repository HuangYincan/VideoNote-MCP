"""小宇宙 FM 下载器占位（未接入 SUPPORT_PLATFORM_MAP）。

上游拷贝时带了一段硬编码 Next.js `_next/data` 的 import-time HTTP GET，
会在任何误 import 时打网。本仓库去掉该副作用；download() 明确未实现。
"""
from typing import Optional, Union

from app.downloaders.base import Downloader
from app.enmus.note_enums import DownloadQuality
from app.models.audio_model import AudioDownloadResult


class Xiaoyuzhoufm_download(Downloader):
    def download(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
        quality: DownloadQuality = "fast",
        need_video: Optional[bool] = False,
    ) -> AudioDownloadResult:
        raise NotImplementedError(
            "小宇宙 FM 下载器尚未接入。请先用 yt-dlp 下到本地，再以 platform='local' 调用。"
        )