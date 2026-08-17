import os

from app.utils.logger import get_logger
from app.utils.path_helper import get_data_dir

logger = get_logger(__name__)

def cleanup_temp_files(data):
    logger.info(f"starting cleanup temp files ：{data['file_path']}")
    file_path = data['file_path']
    if not os.path.exists(file_path):
        logger.warning(f"路径不存在：{file_path}")
        return

    dir_path = os.path.abspath(os.path.dirname(file_path))
    data_dir = os.path.abspath(get_data_dir())
    # 沙箱红线（#127 B6）：只允许清理数据目录内（含任务子目录）的产物。
    # 旧实现只挡「目录 == data/」一种情况，local 直接转写时 dir_path 是用户
    # 源目录（如 ~/Downloads），前缀删除会连带删掉同名 mp4/txt——所有转写器
    # on_finish 已注释、本 handler 是死链，但若将来有人重新接通，这里绝不能
    # 删数据目录外的用户文件。
    if dir_path != data_dir and not dir_path.startswith(data_dir + os.sep):
        logger.warning(f"跳过清理：{dir_path} 不在数据目录内（#127 B6 沙箱红线）")
        return

    base_name = os.path.basename(file_path)
    video_id, _ = os.path.splitext(base_name)

    logger.info(f"开始清理 video_id={video_id} 所有相关文件")

    for file in os.listdir(dir_path):
        if file.startswith(video_id):
            full_path = os.path.join(dir_path, file)
            try:
                os.remove(full_path)
                logger.info(f"删除文件：{full_path}")
            except Exception as e:
                logger.error(f"删除失败：{full_path}，原因：{e}")
