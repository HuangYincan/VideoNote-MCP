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


def _migrate_models_provider_id(engine):
    """把旧 SQLite models.provider_id 迁移为字符串外键并保留已有行。

    ``create_all`` 不会修改已有表，而旧版本建表时把 provider_id 声明成了
    INTEGER 且没有外键。SQLite 不能直接 ALTER COLUMN，因此仅在检测到旧 schema
    时重建 models 表；数据先转成文本再写入新表。历史库若有孤儿 provider_id，
    为其创建 disabled 的占位供应商，避免为了加外键丢弃模型记录。
    """
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as conn:
        table_exists = conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='models'"
        ).first()
        if table_exists is None:
            return

        columns = list(conn.exec_driver_sql("PRAGMA table_info(models)"))
        provider_column = next((row for row in columns if row[1] == "provider_id"), None)
        if provider_column is None:
            return
        foreign_keys = list(conn.exec_driver_sql("PRAGMA foreign_key_list(models)"))
        has_provider_fk = any(
            row[2] == "providers" and row[3] == "provider_id" and row[4] == "id"
            for row in foreign_keys
        )
        provider_type = (provider_column[2] or "").upper()
        if has_provider_fk and provider_type in {"TEXT", "VARCHAR", "CHAR", "CLOB"}:
            return

        # 先把可能存在的孤儿/NULL provider_id 变成可满足外键的占位 id，保留模型行。
        orphan_ids = [
            str(row[0])
            for row in conn.exec_driver_sql(
                """
                SELECT DISTINCT COALESCE(CAST(m.provider_id AS TEXT), '__legacy_unknown__')
                FROM models AS m
                LEFT JOIN providers AS p ON p.id = CAST(m.provider_id AS TEXT)
                WHERE p.id IS NULL
                """
            )
        ]
        for orphan_id in orphan_ids:
            conn.exec_driver_sql(
                """
                INSERT OR IGNORE INTO providers
                    (id, name, logo, type, api_key, base_url, enabled)
                VALUES (?, ?, 'migrated', 'legacy', '', '', 0)
                """,
                (orphan_id, f"迁移占位供应商 {orphan_id}"),
            )

        conn.exec_driver_sql("DROP TABLE IF EXISTS models__provider_id_migration")
        conn.exec_driver_sql(
            """
            CREATE TABLE models__provider_id_migration (
                id INTEGER NOT NULL,
                provider_id VARCHAR NOT NULL,
                model_name VARCHAR NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                FOREIGN KEY(provider_id) REFERENCES providers(id)
            )
            """
        )
        provider_expr = "COALESCE(CAST(provider_id AS TEXT), '__legacy_unknown__')"
        created_expr = "created_at" if any(row[1] == "created_at" for row in columns) else "CURRENT_TIMESTAMP"
        conn.exec_driver_sql(
            f"""
            INSERT INTO models__provider_id_migration (id, provider_id, model_name, created_at)
            SELECT id, {provider_expr}, model_name, {created_expr}
            FROM models
            """
        )
        conn.exec_driver_sql("DROP TABLE models")
        conn.exec_driver_sql("ALTER TABLE models__provider_id_migration RENAME TO models")


def _migrate_video_tasks(engine):
    """给已存在的 video_tasks 表补新增列（create_all 不会改已有表）。

    SQLite 的 ALTER TABLE ADD COLUMN 是幂等的；用 PRAGMA table_info 检查列，
    缺失才补，避免重复 ADD 报错。旧 schema 迁移仅支持 SQLite；其它后端由
    ``create_all`` 负责新库初始化，已有表的迁移交给对应部署的迁移工具。
    """
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as conn:
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
        _migrate_models_provider_id(engine)
    except Exception:
        # 迁移失败时不能以旧 schema 继续启动：create_all 不会修正既有表，
        # 否则后续 DAO 可能在不兼容的结构上运行并造成更隐蔽的数据损坏。
        logger.exception("数据库 schema 迁移失败")
        raise
    _create_indexes(engine)


# 常用查询列索引（docs/05 第 16 轮 C8）：任务量上万后 get_task_by_video /
# get_provider_by_name / get_models_by_provider 从全表扫描降为索引查找。
_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_video_tasks_video ON video_tasks(video_id, platform)",
    "CREATE INDEX IF NOT EXISTS ix_video_tasks_created_at ON video_tasks(created_at)",
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
