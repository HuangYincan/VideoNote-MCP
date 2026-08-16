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

    dir_path = os.path.dirname(file_path)
    # 防御：绝不从共享根目录（data/ 等）按 video_id 前缀批量删除——
    # 并发任务下那会删到别的任务正在用的文件。只在任务自己的子目录里清理。
    if os.path.abspath(dir_path) == os.path.abspath(get_data_dir()):
        logger.warning(f"跳过清理：{dir_path} 是共享数据根目录，不在其中按 video_id 删除")
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
