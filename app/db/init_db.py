import logging

from app.db.engine import Base, get_engine

logger = logging.getLogger(__name__)

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
    _create_indexes(engine)


# 常用查询列索引（docs/05 第 16 轮 C8）：任务量上万后 get_task_by_video /
# get_provider_by_name / get_models_by_provider 从全表扫描降为索引查找。
_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_video_tasks_video ON video_tasks(video_id, platform)",
    "CREATE INDEX IF NOT EXISTS ix_providers_name ON providers(name)",
    "CREATE INDEX IF NOT EXISTS ix_models_provider ON models(provider_id)",
]


def _create_indexes(engine) -> None:
    with engine.connect() as conn:
        for ddl in _INDEXES:
            try:
                conn.exec_driver_sql(ddl)
            except Exception:  # noqa: BLE001 —— 表尚未创建（时序/旧库）幂等跳过，下次 init_db 补建
                logger.warning("建索引失败（表可能尚未创建，跳过）: %s", ddl)
        conn.commit()
