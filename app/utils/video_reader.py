import base64
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
import ffmpeg
from PIL import Image, ImageDraw, ImageFont

from app.utils.logger import get_logger

logger = get_logger(__name__)


def effective_frame_interval(duration: float, video_interval: int, grid_cells: int, max_groups: int = 24) -> int:
    """把网格图组数封顶：超限时自适应拉大截帧间隔（docs/05 #33）。

    1 小时视频 6s 间隔 = 600 帧 ≈ 67 组 3×3 网格图，全组进 LLM 上下文太贵。
    目标 ≤ max_groups 组：间隔 = ceil(duration / (max_groups * grid_cells))。
    duration 未知/异常时原样返回，不干预。
    """
    interval = video_interval or 6
    try:
        if duration <= 0 or grid_cells <= 0:
            return interval
    except TypeError:
        return interval
    max_frames = max_groups * grid_cells
    if duration / interval <= max_frames:
        return interval
    import math

    return max(1, math.ceil(duration / max_frames))


class VideoReader:
    def __init__(self,
                 video_path: str,
                 grid_size=(3, 3),
                 frame_interval=2,
                 dedupe_enabled=True,
                 unit_width=960,
                 unit_height=540,
                 save_quality=90,
                 font_path="fonts/arial.ttf",
                 frame_dir=None,
                 grid_dir=None):
        self.video_path = video_path
        self.grid_size = grid_size
        self.frame_interval = frame_interval
        self.dedupe_enabled = dedupe_enabled
        self.unit_width = unit_width
        self.unit_height = unit_height
        self.save_quality = save_quality
        # 默认 None → run() 内为本次任务创建独立临时目录，
        # 并发任务不再互相清空对方已提取的帧/网格图（旧的共享 output_frames/ 是竞态根因）
        self.frame_dir = frame_dir
        self.grid_dir = grid_dir
        self.font_path = font_path

    @staticmethod
    def _calculate_file_md5(file_path: str) -> str:
        hasher = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def format_time(self, seconds: float) -> str:
        mm = int(seconds // 60)
        ss = int(seconds % 60)
        return f"{mm:02d}_{ss:02d}"

    def extract_time_from_filename(self, filename: str) -> float:
        # \d+ 分钟位：≥100 分钟视频的帧名是 frame_120_00.jpg（旧 \d{2} 只匹配 2 位，
        # 会把 120 误配成 20 → 排序/时间戳错乱）
        match = re.search(r"frame_(\d+)_(\d{2})\.jpg", filename)
        if match:
            mm, ss = map(int, match.groups())
            return mm * 60 + ss
        return float('inf')

    def _extract_single_frame(self, ts: int) -> str | None:
        """提取单帧，返回输出路径或 None（失败时）。"""
        time_label = self.format_time(ts)
        output_path = os.path.join(self.frame_dir, f"frame_{time_label}.jpg")
        cmd = ["ffmpeg", "-ss", str(ts), "-i", self.video_path, "-frames:v", "1", "-q:v", "2", "-y", output_path,
               "-hide_banner", "-loglevel", "error"]
        try:
            subprocess.run(cmd, check=True, timeout=120)
            return output_path
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # TimeoutExpired 未捕获会从 future.result() 冒出，外层兜底把整个抽帧任务
            # 打成「视频处理失败」——已抽出的几百帧全丢（#124 B12）：单帧失败本就该跳过
            return None

    def extract_frames(self, max_frames=1000) -> list[str]:

        try:
            os.makedirs(self.frame_dir, exist_ok=True)
            duration = float(ffmpeg.probe(self.video_path)["format"]["duration"])
            # 帧组数封顶：超限时自适应拉大间隔，而不是截断视频尾部（docs/05 #33）
            cells = self.grid_size[0] * self.grid_size[1]
            self.frame_interval = effective_frame_interval(duration, self.frame_interval, cells)
            timestamps = [i for i in range(0, int(duration), self.frame_interval)][:max_frames]

            # 并行提取帧；len(timestamps)==0（极短/损坏视频）时也要 ≥1，否则 ThreadPoolExecutor(0) 崩溃
            max_workers = max(1, min(os.cpu_count() or 4, 8, len(timestamps)))
            frame_results: dict[int, str | None] = {}
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(self._extract_single_frame, ts): ts for ts in timestamps}
                for future in as_completed(futures):
                    ts = futures[future]
                    frame_results[ts] = future.result()

            # 按时间戳顺序整理结果，并进行去重
            image_paths = []
            last_hash = None
            for ts in timestamps:
                output_path = frame_results.get(ts)
                if not output_path or not os.path.exists(output_path):
                    continue

                if self.dedupe_enabled:
                    frame_hash = self._calculate_file_md5(output_path)
                    if frame_hash == last_hash:
                        os.remove(output_path)
                        continue
                    last_hash = frame_hash

                image_paths.append(output_path)
            return image_paths
        except Exception as e:
            logger.error(f"分割帧发生错误：{str(e)}")
            raise ValueError(f"视频处理失败：{e}") from e

    def group_images(self) -> list[list[str]]:
        image_files = [os.path.join(self.frame_dir, f) for f in os.listdir(self.frame_dir) if
                       f.startswith("frame_") and f.endswith(".jpg")]
        image_files.sort(key=lambda f: self.extract_time_from_filename(os.path.basename(f)))
        group_size = self.grid_size[0] * self.grid_size[1]
        return [image_files[i:i + group_size] for i in range(0, len(image_files), group_size)]

    def concat_images(self, image_paths: list[str], name: str) -> str:
        os.makedirs(self.grid_dir, exist_ok=True)
        font = ImageFont.truetype(self.font_path, 48) if os.path.exists(self.font_path) else ImageFont.load_default()
        images = []

        for path in image_paths:
            img = Image.open(path).convert("RGB").resize((self.unit_width, self.unit_height), Image.Resampling.LANCZOS)
            timestamp = re.search(r"frame_(\d+)_(\d{2})\.jpg", os.path.basename(path))
            time_text = f"{timestamp.group(1)}:{timestamp.group(2)}" if timestamp else ""
            draw = ImageDraw.Draw(img)
            draw.text((10, 10), time_text, fill="yellow", font=font, stroke_width=1, stroke_fill="black")
            images.append(img)

        cols, rows = self.grid_size
        grid_img = Image.new("RGB", (self.unit_width * cols, self.unit_height * rows), (255, 255, 255))

        for i, img in enumerate(images):
            x = (i % cols) * self.unit_width
            y = (i // cols) * self.unit_height
            grid_img.paste(img, (x, y))

        save_path = os.path.join(self.grid_dir, f"{name}.jpg")
        grid_img.save(save_path, quality=self.save_quality)
        return save_path

    def encode_images_to_base64(self, image_paths: list[str]) -> list[str]:
        base64_images = []
        for path in image_paths:
            with open(path, "rb") as img_file:
                encoded_string = base64.b64encode(img_file.read()).decode("utf-8")
                base64_images.append(f"data:image/jpeg;base64,{encoded_string}")
        return base64_images

    def run(self)->list[str]:
        logger.info("开始提取视频帧...")
        # 每个任务独立临时目录：并发任务不再互相清空对方已提取的帧/网格图
        temp_frame_dir = None
        temp_grid_dir = None
        if self.frame_dir is None:
            self.frame_dir = tempfile.mkdtemp(prefix="videonote_frames_")
            temp_frame_dir = self.frame_dir
        if self.grid_dir is None:
            self.grid_dir = tempfile.mkdtemp(prefix="videonote_grid_")
            temp_grid_dir = self.grid_dir
        try:
            # 确保目录存在（显式传入的目录同样保证可用）
            os.makedirs(self.frame_dir, exist_ok=True)
            os.makedirs(self.grid_dir, exist_ok=True)
            self.extract_frames()
            logger.info("开始拼接网格图...")
            image_paths = []
            groups = self.group_images()
            for idx, group in enumerate(groups, start=1):
                if len(group) < self.grid_size[0] * self.grid_size[1]:
                    # 短视频（帧数不足一组）曾被整批跳过 → 静默产出 0 张网格图，
                    # 上层拿到空 frames 当成功。只有这一组时照常拼接（白格兜底），
                    # 已有完整组时才跳过末尾残组（避免稀疏网格图）
                    if image_paths:
                        logger.warning(f"⚠️ 跳过第 {idx} 组，图片不足 {self.grid_size[0] * self.grid_size[1]} 张")
                        continue
                    logger.warning(
                        f"⚠️ 帧数不足一组（{len(group)}/{self.grid_size[0] * self.grid_size[1]}），"
                        "按单组拼接兜底，避免短视频零帧"
                    )
                out_path = self.concat_images(group, f"grid_{idx}")
                image_paths.append(out_path)

            logger.info("📤 开始编码图像...")
            return self.encode_images_to_base64(image_paths)
        except Exception as e:
            logger.error(f"发生错误：{str(e)}")
            raise ValueError(f"视频处理失败：{e}") from e
        finally:
            # 只清理本次任务创建的临时目录；调用方显式传入的目录不动
            if temp_frame_dir:
                shutil.rmtree(temp_frame_dir, ignore_errors=True)
            if temp_grid_dir:
                shutil.rmtree(temp_grid_dir, ignore_errors=True)


