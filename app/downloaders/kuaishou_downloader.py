import os
import subprocess
from abc import ABC
from typing import Union, Optional

import requests

from app.downloaders.base import Downloader
from app.downloaders.kuaishou_helper.kuaishou import KuaiShou
from app.models.audio_model import AudioDownloadResult
from app.utils.logger import get_logger
from app.utils.path_helper import get_data_dir

logger = get_logger(__name__)


class KuaiShouDownloader(Downloader, ABC):
    def __init__(self):
        super().__init__()

    def _download_mp4(self, photo_info: dict, mp4_path: str) -> None:
        """下载 mp4 视频到 mp4_path；HTTP 非 200 抛明确异常。"""
        resp = requests.get(photo_info['photoUrl'], stream=True, timeout=30)
        if resp.status_code == 200:
            with open(mp4_path, "wb") as f:
                for chunk in resp.iter_content(1024 * 1024):
                    f.write(chunk)
        else:
            raise Exception(f"视频下载失败: {resp.status_code}")

    def download(
            self,
            video_url: str,
            output_dir: Union[str, None] = None,
            quality: str = "fast",
            need_video: Optional[bool] = False,
            skip_download: bool = False,
    ) -> AudioDownloadResult:
        if output_dir is None:
            output_dir = get_data_dir()
        if not output_dir:
            output_dir = self.cache_data
        os.makedirs(output_dir, exist_ok=True)

        ks = KuaiShou()
        video_raw_info = ks.run(video_url)
        logger.debug("快手视频原始信息已获取")
        photo_info = video_raw_info['visionVideoDetail']['photo']
        video_id = photo_info['id']
        title = photo_info['caption'].strip().replace('\n', '').replace(' ', '_')[:50]
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

        if os.path.exists(mp3_path):
            logger.info("[已存在] 跳过下载: %s", mp3_path)
            # #123 B9：有 mp3 无 mp4（清理过视频只留音频）时 video_path 悬空——下游
            # VideoReader 拿不存在路径炸「视频处理失败」。mp4 缺失则补下。
            if not os.path.exists(mp4_path):
                logger.info("[已存在] mp3 命中但 mp4 缺失，重新下载视频: %s", mp4_path)
                self._download_mp4(photo_info, mp4_path)
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
        self._download_mp4(photo_info, mp4_path)

        # 使用 ffmpeg 转换为 mp3
        try:
            subprocess.run([
                "ffmpeg", "-y", "-i", mp4_path, "-vn", "-acodec", "libmp3lame", mp3_path
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise Exception("ffmpeg 转换 MP3 失败")

        return AudioDownloadResult(
            file_path=mp3_path,
            title=photo_info['caption'],
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
    ) -> str:
        result = self.download(video_url, output_dir)
        return result.video_path


if __name__ == '__main__':
    ks = KuaiShouDownloader()
    ks.download('https://v.kuaishou.com/2vBqX74 王宝强携手刘昊然、岳云鹏上演精彩名场面 全程高能 看一遍笑一遍 "唐探1900 "快成长计划 ...更多')