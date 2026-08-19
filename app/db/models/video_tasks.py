from sqlalchemy import Column, DateTime, Integer, String, func

from app.db.engine import Base


class VideoTask(Base):
    __tablename__ = "video_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    video_id = Column(String, nullable=False)
    platform = Column(String, nullable=False)
    task_id = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    # —— 数据层重构新增：全局任务索引的语义元数据 ——
    title = Column(String, default="", nullable=True)        # 语义标题（视频标题 / LLM 标题）
    status = Column(String, default="", nullable=True)       # 最新状态（SUCCESS/FAILED/CANCELLED…）
    summary = Column(String, default="", nullable=True)      # 语义简介（转写前若干字）
    note_dir = Column(String, nullable=True)                 # 任务文件夹路径（note_results/{task_id}）