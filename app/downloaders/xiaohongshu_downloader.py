"""小红书下载器：解析笔记页直链，下载视频并抽音频。

登录态走 `xiaohongshu` cookie 槽（扫码优先）。图文笔记明确报错。
无官方字幕（download_subtitles 返回 None），流水线走本地 ASR。
"""
from __future__ import annotations

import os
import subprocess
import threading
from typing import Optional, Union

from app.downloaders.base import Downloader
from app.downloaders.common import stream_download
from app.downloaders.xiaohongshu_auth import XiaohongshuAuth, XiaohongshuNote
from app.enmus.note_enums import DownloadQuality
from app.exceptions.task import TaskCancelledError, check_cancel
from app.models.audio_model import AudioDownloadResult
from app.utils.logger import get_logger
from app.utils.path_helper import get_data_dir
from app.utils.url_parser import extract_video_id
from app.utils.url_safety import assert_public_http_url, sanitize_url

logger = get_logger(__name__)

_PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.xiaohongshu.com/",
    "Accept": "*/*",
}


class XiaohongshuDownloader(Downloader):
    def __init__(self, auth: Optional[XiaohongshuAuth] = None):
        super().__init__()
        self._auth = auth
        self._owns_auth = auth is None

    def _get_auth(self) -> XiaohongshuAuth:
        if self._auth is None:
            self._auth = XiaohongshuAuth()
            self._owns_auth = True
        return self._auth

    def close(self) -> None:
        if self._owns_auth and self._auth is not None:
            try:
                self._auth.close()
            except Exception:
                pass
            self._auth = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _fetch(self, video_url: str) -> XiaohongshuNote:
        note = self._get_auth().fetch_note(video_url)
        if not note.video_url:
            raise RuntimeError(
                f"该笔记不是视频（图文笔记无法转写）: {sanitize_url(note.page_url or video_url)}"
            )
        return note

    def _download_mp4(
        self,
        note: XiaohongshuNote,
        mp4_path: str,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        assert_public_http_url(note.video_url)
        stream_download(
            note.video_url,
            mp4_path,
            headers=_PAGE_HEADERS,
            cancel_event=cancel_event,
        )
        if not os.path.exists(mp4_path) or os.path.getsize(mp4_path) <= 0:
            raise RuntimeError(f"小红书视频下载后文件为空: {mp4_path}")

    def _to_mp3(
        self,
        mp4_path: str,
        mp3_path: str,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        if os.path.exists(mp3_path):
            try:
                os.unlink(mp3_path)
            except OSError:
                pass
        import time as _time

        cmd = ["ffmpeg", "-y", "-i", mp4_path, "-vn", "-acodec", "libmp3lame", mp3_path]
        check_cancel(cancel_event)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise RuntimeError("启动 ffmpeg 转换 MP3 失败") from exc
        deadline = _time.monotonic() + 600
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise TaskCancelledError("任务已取消")
            if _time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                raise RuntimeError("ffmpeg 转换 MP3 超时")
            if cancel_event is not None and cancel_event.wait(0.2):
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise TaskCancelledError("任务已取消")
            elif cancel_event is None:
                _time.sleep(0.2)
        check_cancel(cancel_event)
        if proc.returncode != 0 or not os.path.exists(mp3_path) or os.path.getsize(mp3_path) <= 0:
            raise RuntimeError(
                f"ffmpeg 转换 MP3 失败（退出码 {proc.returncode}）"
            )

    def _result(self, note: XiaohongshuNote, mp3_path: str, mp4_path: Optional[str]) -> AudioDownloadResult:
        return AudioDownloadResult(
            file_path=mp3_path,
            title=note.title or note.note_id,
            duration=note.duration,
            cover_url=note.cover_url,
            platform="xiaohongshu",
            video_id=note.note_id,
            raw_info={"desc": note.desc, "page_url": note.page_url},
            video_path=mp4_path,
        )

    def download(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
        quality: DownloadQuality = "fast",
        need_video: Optional[bool] = False,
        skip_download: bool = False,
        cancel_event: Optional[threading.Event] = None,
    ) -> AudioDownloadResult:
        check_cancel(cancel_event)
        assert_public_http_url(video_url)
        if output_dir is None:
            output_dir = get_data_dir()
        if not output_dir:
            output_dir = self.cache_data
        os.makedirs(output_dir, exist_ok=True)

        try:
            note = self._fetch(video_url)
        except TaskCancelledError:
            raise
        except Exception as exc:
            if skip_download and "不是视频" not in str(exc):
                nid = extract_video_id(video_url, "xiaohongshu")
                if nid:
                    logger.warning("缓存命中路径获取笔记页失败，使用 URL 存根元信息: %s", exc)
                    return AudioDownloadResult(
                        file_path=os.path.join(output_dir, f"{nid}.mp3"),
                        title=nid,
                        duration=0.0,
                        cover_url=None,
                        platform="xiaohongshu",
                        video_id=nid,
                        raw_info={"page_url": video_url},
                        video_path=None,
                    )
            raise
        mp4_path = os.path.join(output_dir, f"{note.note_id}.mp4")
        mp3_path = os.path.join(output_dir, f"{note.note_id}.mp3")

        if skip_download:
            return self._result(note, mp3_path, None)

        if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0:
            logger.info("[已存在] 跳过下载: %s", mp3_path)
            if not os.path.exists(mp4_path) or os.path.getsize(mp4_path) <= 0:
                self._download_mp4(note, mp4_path, cancel_event=cancel_event)
            return self._result(note, mp3_path, mp4_path)

        self._download_mp4(note, mp4_path, cancel_event=cancel_event)
        self._to_mp3(mp4_path, mp3_path, cancel_event=cancel_event)
        check_cancel(cancel_event)
        return self._result(note, mp3_path, mp4_path)

    def download_video(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        check_cancel(cancel_event)
        assert_public_http_url(video_url)
        if output_dir is None:
            output_dir = get_data_dir()
        if not output_dir:
            output_dir = self.cache_data
        os.makedirs(output_dir, exist_ok=True)
        note = self._fetch(video_url)
        mp4_path = os.path.join(output_dir, f"{note.note_id}.mp4")
        if os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0:
            return mp4_path
        if os.path.exists(mp4_path):
            try:
                os.unlink(mp4_path)
            except OSError:
                pass
        self._download_mp4(note, mp4_path, cancel_event=cancel_event)
        return mp4_path
