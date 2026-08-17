# 注册监听器
from app.events.handlers import cleanup_temp_files
from app.events.signals import transcription_finished
from app.utils.logger import get_logger

logger = get_logger(__name__)

def  register_handler():
    try:
        transcription_finished.connect(cleanup_temp_files)
        logger.info("注册监听器成功")
    except Exception as e:
        logger.error(f"注册监听器失败:{e}")

