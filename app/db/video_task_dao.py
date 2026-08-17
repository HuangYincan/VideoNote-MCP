from typing import Optional

from app.db.models.video_tasks import VideoTask
from app.db.engine import get_db
from app.utils.logger import get_logger

logger = get_logger(__name__)


# 插入/更新任务（task_id 冲突时更新语义元数据，而非抛错）
def insert_video_task(
    video_id: str,
    platform: str,
    task_id: str,
    title: str = "",
    status: str = "",
    summary: str = "",
    note_dir: str = None,
):
    db = next(get_db())
    try:
        existing = db.query(VideoTask).filter_by(task_id=task_id).first()
        if existing:
            existing.video_id = video_id
            existing.platform = platform
            if title:
                existing.title = title
            if status:
                existing.status = status
            if summary:
                existing.summary = summary
            if note_dir is not None:
                existing.note_dir = note_dir
        else:
            db.add(
                VideoTask(
                    video_id=video_id,
                    platform=platform,
                    task_id=task_id,
                    title=title,
                    status=status,
                    summary=summary,
                    note_dir=note_dir,
                )
            )
        db.commit()
        logger.info(f"Video task saved. task_id={task_id}, title={title[:40]!r}")
    except Exception as e:
        logger.error(f"Failed to insert video task: {e}")
    finally:
        db.close()


# 更新任务状态（每次 _update_status 时同步到全局索引）
def update_task_status(task_id: str, status: str, message: str = ""):
    db = next(get_db())
    try:
        task = db.query(VideoTask).filter_by(task_id=task_id).first()
        if task:
            task.status = status
            if message and not task.summary:
                task.summary = message
        else:
            logger.warning(f"update_task_status: task {task_id} 不在全局索引，跳过")
        db.commit()
    except Exception as e:
        logger.error(f"Failed to update task status: {e}")
    finally:
        db.close()


# 列出全部任务（全局索引），供 list_tasks 工具 / setup 数据管理
def list_tasks(limit: Optional[int] = None, offset: int = 0) -> list:
    db = next(get_db())
    try:
        # 分页下推到 SQL（LIMIT/OFFSET），任务量大的会话不再全表拉回 Python 层切片
        query = db.query(VideoTask).order_by(VideoTask.created_at.desc()).offset(max(0, int(offset or 0)))
        if limit is not None:
            query = query.limit(max(1, int(limit)))
        rows = query.all()
        return [
            {
                "task_id": r.task_id,
                "video_id": r.video_id,
                "platform": r.platform,
                "title": r.title or "",
                "status": r.status or "",
                "summary": r.summary or "",
                "note_dir": r.note_dir,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"Failed to list tasks: {e}")
        return []
    finally:
        db.close()


# 按 task_id 删一条全局索引（清理任务时调用）
def delete_task(task_id: str):
    db = next(get_db())
    try:
        task = db.query(VideoTask).filter_by(task_id=task_id).first()
        if task:
            db.delete(task)
            db.commit()
            logger.info(f"Task deleted from index: {task_id}")
    except Exception as e:
        logger.error(f"Failed to delete task: {e}")
    finally:
        db.close()


# 查询任务（最新一条）
def get_task_by_video(video_id: str, platform: str):
    db = next(get_db())
    try:
        task = (
            db.query(VideoTask)
            .filter_by(video_id=video_id, platform=platform)
            .order_by(VideoTask.created_at.desc())
            .first()
        )
        if task:
            logger.info(f"Task found for video_id: {video_id} and platform: {platform}")
            return task.task_id
        else:
            logger.info(f"No task found for video_id: {video_id} and platform: {platform}")
            return None
    except Exception as e:
        logger.error(f"Failed to get task by video: {e}")
    finally:
        db.close()


# 删除任务
def delete_task_by_video(video_id: str, platform: str):
    db = next(get_db())
    try:
        tasks = (
            db.query(VideoTask)
            .filter_by(video_id=video_id, platform=platform)
            .all()
        )
        for task in tasks:
            db.delete(task)
        db.commit()
        logger.info(f"Task(s) deleted for video_id: {video_id} and platform: {platform}")
    except Exception as e:
        logger.error(f"Failed to delete task by video: {e}")
    finally:
        db.close()
