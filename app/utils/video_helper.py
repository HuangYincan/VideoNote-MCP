import shutil
from pathlib import Path

import subprocess
import os
import uuid

from typing import Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)
def generate_screenshot(video_path: str, output_dir: str, timestamp: int, index: int) -> str:
    """
    使用 ffmpeg 生成截图，返回生成图片路径
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    filename = f"screenshot_{index:03}_{uuid.uuid4()}.jpg"
    output_path = output_dir / filename

    command = [
        "ffmpeg",
        "-ss", str(timestamp),
        "-i", str(video_path),
        "-frames:v", "1",
        "-q:v", "2",
        str(output_path),
        "-y"
    ]

    logger.debug("Running command: %s", command)
    result = subprocess.run(command, capture_output=True, text=True)

    if result.returncode != 0:
        logger.warning("ffmpeg failed: %s", result.stderr)

    return str(output_path)



def _static_root() -> str:
    """静态资源根：MCP 数据目录（隔离，避免写进仓库 CWD）；否则 CWD/static（兼容旧行为）。"""
    data_dir = os.getenv("VIDEONOTE_DATA_DIR")
    if data_dir:
        return os.path.join(data_dir, "static")
    return os.path.join(os.getcwd(), "static")


def save_cover_to_static(local_cover_path: str, subfolder: Optional[str] = "cover") -> str:
    """
    将封面图片保存到 static 目录下，并返回前端可访问的路径
    :param local_cover_path: 本地原封面路径（比如提取出来的jpg）
    :param subfolder: 子目录，默认是 cover，可以自定义
    :return: 前端访问路径，例如 /static/cover/xxx.jpg
    """
    # static 目录：MCP 数据目录（隔离）；无 env 时用 CWD（兼容 CLI/旧行为）
    static_dir = _static_root()

    # 确定目标子目录
    target_dir = os.path.join(static_dir, subfolder or "cover")
    os.makedirs(target_dir, exist_ok=True)

    # 拷贝文件
    file_name = os.path.basename(local_cover_path)
    target_path = os.path.join(target_dir, file_name)
    shutil.copy2(local_cover_path, target_path)  # 保留原时间戳、权限
    # 返回 file:// 绝对路径（agent 可直接 Read；无后端可指）
    return Path(target_path).as_uri()
