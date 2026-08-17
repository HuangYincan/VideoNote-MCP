import hashlib
import os
import subprocess
from abc import ABC
from typing import Optional

from app.downloaders.base import Downloader
from app.enmus.note_enums import DownloadQuality
from app.models.audio_model import AudioDownloadResult
from app.utils.logger import get_logger
from app.utils.video_helper import save_cover_to_static

logger = get_logger(__name__)


class LocalDownloader(Downloader, ABC):
    def __init__(self):

        super().__init__()


    def extract_cover(self, input_path: str, output_dir: Optional[str] = None) -> str:
        """
        从本地视频文件中提取一张封面图（默认取第一帧）
        :param input_path: 输入视频路径
        :param output_dir: 输出目录，默认和视频同目录
        :return: 提取出的封面图片路径
        """
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"输入文件不存在: {input_path}")

        if output_dir is None:
            # 封面是中间产物，写数据目录而非用户媒体目录（避免源目录污染）
            from app.utils.path_helper import get_data_dir

            output_dir = os.path.join(get_data_dir(), "covers")

        base_name = os.path.splitext(os.path.basename(input_path))[0]
        # 文件名带输入路径短 hash：两个不同目录同名视频（/a/clip.mp4 与 /b/clip.mp4）
        # 此前产出同名封面互相覆盖，先前笔记已嵌入的 file:// 引用会静默变成另一视频
        # 的封面（#124 B11）；同一路径重复处理仍同名（幂等覆盖，符合预期）
        digest = hashlib.sha256(input_path.encode("utf-8")).hexdigest()[:8]
        output_path = os.path.join(output_dir, f"{base_name}_{digest}_cover.jpg")

        # covers 目录此前从不创建 → ffmpeg 写失败被吞、cover_url 恒为空（#121 B3）
        os.makedirs(output_dir, exist_ok=True)

        try:
            command = [
                'ffmpeg',
                '-i', input_path,
                '-ss', '00:00:01',  # 跳到视频第1秒，防止黑屏
                '-vframes', '1',  # 只截取一帧
                '-q:v', '2',  # 输出质量高一点（qscale，2是很高）
                '-y',  # 覆盖
                output_path
            ]
            subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=600)

            if not os.path.exists(output_path):
                raise RuntimeError(f"封面图片生成失败: {output_path}")

            return output_path
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"提取封面失败: {output_path}") from e

    def convert_to_mp3(self,input_path: str, output_path: str = None) -> str:
        """
        将本地视频文件转为 MP3 音频文件
        :param input_path: 输入文件路径（如 .mp4）
        :param output_path: 输出文件路径（可选，默认同目录同名 .mp3）
        :return: 生成的 mp3 文件路径
        """
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"输入文件不存在: {input_path}")

        if output_path is None:
            # 缺省落数据目录而非源文件同目录（#127 B9）：源目录不污染、归 cleanup 管；
            # note.py 主路径总传 output_dir，此分支仅外部直接调用时生效
            from app.utils.path_helper import get_data_dir

            base_name = os.path.splitext(os.path.basename(input_path))[0]
            output_path = os.path.join(get_data_dir(), f"{base_name}.mp3")
        try:
        # 调用 ffmpeg 转换
            command = [
                'ffmpeg',
                '-i', input_path,
                '-vn',  # 不要视频流
                '-acodec', 'libmp3lame',  # 使用mp3编码
                '-y',  # 覆盖输出文件
                output_path
            ]

            subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=600)

            if not os.path.exists(output_path):
                raise RuntimeError(f"mp3 文件生成失败: {output_path}")

            return output_path
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"mp3 文件生成失败: {output_path}") from e
    def download_video(self, video_url: str, output_dir: str = None) -> str:
        """
        处理本地文件路径，返回视频文件路径
        """
        if video_url.startswith('/uploads'):
            project_root = os.getcwd()
            video_url = os.path.join(project_root, video_url.lstrip('/'))
            video_url = os.path.normpath(video_url)

        if not os.path.exists(video_url):
            raise FileNotFoundError()
        return video_url
    def download(
            self,
            video_url: str,
            output_dir: str = None,
            quality: DownloadQuality = "fast",
            need_video: Optional[bool] = False,
            skip_download: bool = False
    ) -> AudioDownloadResult:
        """
        处理本地文件路径，返回音频元信息
        """
        if video_url.startswith('/uploads'):
            project_root = os.getcwd()
            video_url = os.path.join(project_root, video_url.lstrip('/'))
            video_url = os.path.normpath(video_url)

        if not os.path.exists(video_url):
            raise FileNotFoundError(f"本地文件不存在: {video_url}")

        file_name = os.path.basename(video_url)
        title, _ = os.path.splitext(file_name)
        # 尊重 output_dir：本地文件并发任务各写各的 mp3，不再默认写到源视频同目录
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            mp3_out = os.path.join(output_dir, f"{title}.mp3")
        else:
            mp3_out = None
        if skip_download:
            # 只需元信息：不转 mp3，file_path 直接指向源文件
            return AudioDownloadResult(
                file_path=video_url,
                title=title,
                duration=0,
                cover_url="",
                platform="local",
                video_id=title,
                raw_info={'path': video_url},
                video_path=video_url,
            )
        file_path = self.convert_to_mp3(video_url, mp3_out)
        # 封面提取对纯音频文件（mp3/wav 等）不适用；失败不阻断笔记生成
        cover_url = ""
        try:
            cover_path = self.extract_cover(video_url)
            cover_url = save_cover_to_static(cover_path)
        except Exception as e:
            logger.warning(f"提取封面失败（忽略）: {e}")

        return AudioDownloadResult(
            file_path=file_path,
            title=title,
            duration=0,  # 可选：后续加上读取时长
            cover_url=cover_url,  # 暂无封面
            platform="local",
            video_id=title,
            raw_info={
                'path':  file_path
            },
            video_path=None
        )
