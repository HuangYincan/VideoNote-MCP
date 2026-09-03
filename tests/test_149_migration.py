"""#149 SQLite 旧 schema 迁移与失败可见性回归。"""
import importlib
from unittest import mock

import pytest
from sqlalchemy import create_engine


init_db_module = importlib.import_module("app.db.init_db")


def _legacy_engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")


def test_models_provider_id_migration_preserves_rows_and_orphans(tmp_path):
    engine = _legacy_engine(tmp_path)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE providers (
                    id VARCHAR PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    logo VARCHAR,
                    type VARCHAR,
                    api_key VARCHAR,
                    base_url VARCHAR,
                    enabled INTEGER
                )
                """
            )
            conn.exec_driver_sql(
                """
                CREATE TABLE models (
                    id INTEGER PRIMARY KEY,
                    provider_id INTEGER,
                    model_name VARCHAR NOT NULL,
                    created_at DATETIME
                )
                """
            )
            conn.exec_driver_sql(
                "INSERT INTO providers (id, name, enabled) VALUES ('1', '已存在', 1)"
            )
            conn.exec_driver_sql(
                """
                INSERT INTO models (id, provider_id, model_name)
                VALUES (1, 1, 'valid'), (2, 999, 'orphan'), (3, NULL, 'null-provider')
                """
            )

        init_db_module._migrate_models_provider_id(engine)

        with engine.connect() as conn:
            columns = list(conn.exec_driver_sql("PRAGMA table_info(models)"))
            foreign_keys = list(conn.exec_driver_sql("PRAGMA foreign_key_list(models)"))
            rows = conn.exec_driver_sql(
                "SELECT id, provider_id, model_name FROM models ORDER BY id"
            ).all()
            placeholders = conn.exec_driver_sql(
                "SELECT id, enabled FROM providers WHERE id IN ('999', '__legacy_unknown__')"
            ).all()

        provider_column = next(row for row in columns if row[1] == "provider_id")
        assert provider_column[2].upper() == "VARCHAR"
        assert provider_column[3] == 1
        assert any(
            row[2] == "providers" and row[3] == "provider_id" and row[4] == "id"
            for row in foreign_keys
        )
        assert rows == [
            (1, "1", "valid"),
            (2, "999", "orphan"),
            (3, "__legacy_unknown__", "null-provider"),
        ]
        assert sorted(placeholders) == [("999", 0), ("__legacy_unknown__", 0)]

        # 已迁移 schema 再运行应保持幂等，不重复创建占位 provider 或重建表。
        init_db_module._migrate_models_provider_id(engine)
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM models").scalar() == 3
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM providers").scalar() == 3
    finally:
        engine.dispose()


def test_video_tasks_migration_commits_and_is_idempotent(tmp_path):
    engine = _legacy_engine(tmp_path)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE video_tasks (
                    id INTEGER PRIMARY KEY,
                    video_id VARCHAR,
                    platform VARCHAR,
                    task_id VARCHAR,
                    created_at DATETIME
                )
                """
            )

        init_db_module._migrate_video_tasks(engine)
        init_db_module._migrate_video_tasks(engine)

        with engine.connect() as conn:
            columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(video_tasks)")}
        assert {"title", "status", "summary", "note_dir"} <= columns
    finally:
        engine.dispose()


def test_init_db_reraises_schema_migration_failure():
    module = init_db_module
    with mock.patch.object(module.Base.metadata, "create_all"), mock.patch.object(
        module, "get_engine", return_value=mock.Mock()
    ), mock.patch.object(
        module, "_migrate_video_tasks", side_effect=RuntimeError("migration failed")
    ), mock.patch.object(module, "_create_indexes") as create_indexes:
        with pytest.raises(RuntimeError, match="migration failed"):
            module.init_db()
    create_indexes.assert_not_called()
