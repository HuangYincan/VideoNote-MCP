from typing import Optional

from app.db.engine import get_db
from app.db.models.video_tasks import VideoTask
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
        # title 可能是 None（无标题视频/平台），f-string 里切片会 TypeError——
        # 行已入库却被记成「插入失败」，排查指向错误方向（#126 B8）
        logger.info(f"Video task saved. task_id={task_id}, title={(title or '')[:40]!r}")
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
            # 占位文案（"任务排队中"）只作状态消息、不落 summary（docs/05 第 16 轮 B7）：
            # 步骤任务（transcribe_media 等）成功后 summary 曾恒为"任务排队中"误导 Agent。
            # 语义简介由调用方显式写（_save_metadata / insert_video_task）。
            if message and not task.summary and message != "任务排队中":
                task.summary = message
            if status == "SUCCESS" and task.summary == "任务排队中":
                task.summary = ""
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


# 清空全局索引（cleanup_all 用）：单条 DELETE 替代 list + N 条单删（#125 B12）。
# 返回受影响行数；失败时抛给调用方显式处理（空表不是失败）。调用方保证只在
# 显式 cleanup_all 语义下调用，绝不因卸载/升级触发。
def delete_all_tasks() -> int:
    db = next(get_db())
    try:
        deleted = db.query(VideoTask).delete(synchronize_session=False)
        db.commit()
        logger.info(f"Cleared task index: {deleted} rows")
        return deleted
    except Exception as e:
        logger.error(f"Failed to clear task index: {e}")
        raise
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
