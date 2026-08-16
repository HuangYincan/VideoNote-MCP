from app.db.engine import get_engine, Base

# video_tasks 表在数据层重构中新增的列（SQLite ALTER 幂等迁移用）
_VIDEO_TASK_MIGRATIONS = [
    ("title", "VARCHAR"),
    ("status", "VARCHAR"),
    ("summary", "VARCHAR"),
    ("note_dir", "VARCHAR"),
]


def _migrate_video_tasks(engine):
    """给已存在的 video_tasks 表补新增列（create_all 不会改已有表）。

    SQLite 的 ALTER TABLE ADD COLUMN 是幂等的；用 PRAGMA table_info 检查列，
    缺失才补，避免重复 ADD 报错。
    """

    with engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(video_tasks)")}
        for name, typ in _VIDEO_TASK_MIGRATIONS:
            if name not in cols:
                conn.exec_driver_sql(
                    f"ALTER TABLE video_tasks ADD COLUMN {name} {typ}"
                )


def init_db():
    engine = get_engine()

    Base.metadata.create_all(bind=engine)
    try:
        _migrate_video_tasks(engine)
    except Exception:
        # 迁移失败不致命（表可能不存在 / 已是最新），保底 create_all 已建表
        pass
