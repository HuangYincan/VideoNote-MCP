import os
import subprocess
import threading
from abc import ABC
from typing import Optional, Union

from app.downloaders.base import Downloader
from app.downloaders.common import stream_download
from app.downloaders.kuaishou_helper.kuaishou import KuaiShou
from app.models.audio_model import AudioDownloadResult
from app.utils.logger import get_logger
from app.utils.path_helper import get_data_dir

logger = get_logger(__name__)


class KuaiShouDownloader(Downloader, ABC):
    def __init__(self):
        super().__init__()

    @staticmethod
    def _extract_photo(video_raw_info: dict) -> dict:
        """从快手接口返回里取 photo 详情；形状变更/视频被删时给可排查的明确错误。

        接口返回嵌套 `{'visionVideoDetail': {'photo': {...}}}`。visionVideoDetail 为
        null 或 photo 缺失（视频被删/接口变更）时，裸索引抛 `None['photo']` 天书
        TypeError——任务整体 FAILED 且无法定位（#127 B5 修了 caption，这里是父级）。
        """
        detail = video_raw_info.get("visionVideoDetail") or {}
        photo = detail.get("photo")
        if not isinstance(photo, dict):
            raise RuntimeError(
                "快手接口返回缺少 visionVideoDetail.photo（视频可能已删除，或接口形状变更）"
            )
        if not photo.get("id"):
            raise RuntimeError("快手接口返回缺少 photo.id")
        return photo

    def _download_mp4(
        self, photo_info: dict, mp4_path: str, cancel_event: Optional[threading.Event] = None
    ) -> None:
        """下载 mp4 视频到 mp4_path；HTTP 非 200 抛明确异常。"""
        photo_url = photo_info.get("photoUrl")
        if not photo_url:
            raise RuntimeError("快手接口返回缺少 photoUrl（下载地址）")
        # 连接/读分离超时 + 退避重试 + 取消检查（docs/05 第 16 轮 B4/B1）；
        # with 托管响应关闭：裸 requests.get 的 stream 响应直到 GC 才释放连接
        #（每次下载泄漏一个连接池条目，#124 B8）
        stream_download(
            photo_url, mp4_path, cancel_event=cancel_event
        )

    def download(
            self,
            video_url: str,
            output_dir: Union[str, None] = None,
            quality: str = "fast",
            need_video: Optional[bool] = False,
            skip_download: bool = False,
            cancel_event: Optional[threading.Event] = None,
    ) -> AudioDownloadResult:
        if output_dir is None:
            output_dir = get_data_dir()
        if not output_dir:
            output_dir = self.cache_data
        os.makedirs(output_dir, exist_ok=True)

        ks = KuaiShou()
        video_raw_info = ks.run(video_url)
        logger.debug("快手视频原始信息已获取")
        photo_info = KuaiShouDownloader._extract_photo(video_raw_info)
        video_id = photo_info["id"]
        # caption 可为 null（无文案/草稿）——裸 strip 会在下载开始前 AttributeError（#127 B5）
        title = (photo_info.get('caption') or '').strip().replace('\n', '').replace(' ', '_')[:50]
        mp4_path = os.path.join(output_dir, f"{video_id}.mp4")
        mp3_path = os.path.join(output_dir, f"{video_id}.mp3")

        if skip_download:
            # 已有字幕只需元信息：不下载 mp4、不转 mp3
            return AudioDownloadResult(
                file_path=mp3_path,
                title=title,
                duration=photo_info['duration'],
                cover_url=photo_info['coverUrl'],
                platform="kuaishou",
                video_id=video_id,
                raw_info={
                    'tags': ','.join(tag['name'] for tag in video_raw_info.get('tags', []) if tag.get('name'))
                },
                video_path=None
            )

        if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0:
            logger.info("[已存在] 跳过下载: %s", mp3_path)
            # #123 B9：有 mp3 无 mp4（清理过视频只留音频）时 video_path 悬空——下游
            # VideoReader 拿不存在路径炸「视频处理失败」。mp4 缺失则补下。
            # #124 B1：零字节/半成品 mp3（ffmpeg 中断残留）不被 exists 当成功产物。
            if not os.path.exists(mp4_path):
                logger.info("[已存在] mp3 命中但 mp4 缺失，重新下载视频: %s", mp4_path)
                self._download_mp4(photo_info, mp4_path, cancel_event=cancel_event)
            return AudioDownloadResult(
                file_path=mp3_path,
                title=title,
                duration=photo_info['duration'],
                cover_url=photo_info['coverUrl'],
                platform="kuaishou",
                video_id=video_id,
                raw_info={
                    'tags': ','.join(tag['name'] for tag in video_raw_info.get('tags', []) if tag.get('name'))
                },
                video_path=mp4_path
            )

        # 下载 mp4 视频
        self._download_mp4(photo_info, mp4_path, cancel_event=cancel_event)

        # 使用 ffmpeg 转换为 mp3
        try:
            # 转前先清残留：`-y` 中途超时/报错会留下半成品 mp3，下次 exists 命中把
            # 损坏音频当成功产物（#124 B1）
            if os.path.exists(mp3_path):
                os.unlink(mp3_path)
            subprocess.run([
                "ffmpeg", "-y", "-i", mp4_path, "-vn", "-acodec", "libmp3lame", mp3_path
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            # 带原始异常链与退出码：后台任务只看到一句话无法区分超时/非零退出（#125 B11）
            detail = getattr(e, "returncode", None)
            raise Exception(f"ffmpeg 转换 MP3 失败（{'退出码 ' + str(detail) if detail is not None else '超时'}）") from e

        return AudioDownloadResult(
            file_path=mp3_path,
            title=title,
            duration=photo_info['duration'],
            cover_url=photo_info['coverUrl'],
            platform="kuaishou",
            video_id=video_id,
            raw_info={
                'tags': ','.join(tag['name'] for tag in video_raw_info.get('tags', []) if tag.get('name'))
            },
            video_path=mp4_path
        )

    def download_video(
            self,
            video_url: str,
            output_dir: Union[str, None] = None,
            cancel_event: Optional[threading.Event] = None,
    ) -> str:
        # 只下载视频、不转 mp3：need_video 场景（截图/视频理解）之后会从 mp4 提取
        # 音频；旧实现走完整 download() 必然白做一次全量 ffmpeg 转码 + 双份磁盘
        # 占用（#125 B2）。与其他 downloader 的 download_video 语义一致。
        if output_dir is None:
            output_dir = get_data_dir()
        if not output_dir:
            output_dir = self.cache_data
        os.makedirs(output_dir, exist_ok=True)
        ks = KuaiShou()
        video_raw_info = ks.run(video_url)
        photo_info = KuaiShouDownloader._extract_photo(video_raw_info)
        mp4_path = os.path.join(output_dir, f"{photo_info['id']}.mp4")
        self._download_mp4(photo_info, mp4_path, cancel_event=cancel_event)
        return mp4_path


if __name__ == '__main__':
    ks = KuaiShouDownloader()
    ks.download('https://v.kuaishou.com/2vBqX74 王宝强携手刘昊然、岳云鹏上演精彩名场面 全程高能 看一遍笑一遍 "唐探1900 "快成长计划 ...更多')