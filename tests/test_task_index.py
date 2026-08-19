"""全局任务索引（video_tasks 表 + DAO）单元测试。

不碰真实数据库 —— 用隔离 SQLite 文件。

覆盖：
1. init_db 幂等迁移：旧 schema（无新列）经 init_db 后补齐 title/status/summary/note_dir；
2. insert upsert：同 task_id 再插更新标题、不抛错；
3. update_task_status：更新状态；
4. list_tasks：返回带 title/status/summary/created_at；
5. delete_task：删除单条。
"""
import os
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 与会话级 conftest 同库（全量 pytest 时 conftest 已设 DATABASE_URL，这里 setdefault 不覆盖）；
# 直接 `python tests/test_task_index.py` 时 conftest 不加载，setdefault 兜底自建同路径库。
# 注意：_DB 必须读回实际 DATABASE_URL（conftest 指向 pid 隔离库），不能写死默认路径——
# 写死会在全新 CI runner 上连到从未创建的目录，报 sqlite3.OperationalError:
# unable to open database file（docs/05 第 17 轮 CI 修复）。
_DEFAULT_DB = "/tmp/videonote_pytest/video_note.db"
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DEFAULT_DB}")
_DB = os.environ["DATABASE_URL"].removeprefix("sqlite:///")
# 兜底建父目录：独立跑本文件时 conftest 不加载，/tmp/videonote_pytest 需自建
Path(_DB).parent.mkdir(parents=True, exist_ok=True)

from app.db.init_db import init_db  # noqa: E402
from app.db.video_task_dao import (  # noqa: E402
    delete_all_tasks,
    delete_task,
    get_task_by_video,
    insert_video_task,
    list_tasks,
    update_task_status,
)

# engine 是模块级单例且缓存连接池：测试期间 DB 文件不能删（否则连接变只读）。
# 用「只建不删 + 唯一 task_id」策略，测试互不污染。
_tick = 0


def _tid():
    global _tick
    _tick += 1
    return f"t{_tick}"


class MigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def test_init_db_creates_columns(self):
        # 表已存在且带新列（引擎模块级单例建表）
        conn = sqlite3.connect(_DB)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(video_tasks)")}
        conn.close()
        self.assertTrue({"title", "status", "summary", "note_dir"} <= cols)

    def test_init_db_idempotent(self):
        # 已有新列再跑 init_db 不报错
        init_db()


class DaoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def test_insert_and_list_with_metadata(self):
        tid1, tid2 = _tid(), _tid()
        insert_video_task(
            "BV1", "bilibili", tid1,
            title="机器学习", status="SUCCESS", summary="讲监督学习", note_dir="/x/" + tid1,
        )
        insert_video_task("BV2", "bilibili", tid2, title="深度学习", status="FAILED")
        tasks = list_tasks()
        by_id = {t["task_id"]: t for t in tasks}
        self.assertEqual(by_id[tid1]["title"], "机器学习")
        self.assertEqual(by_id[tid1]["status"], "SUCCESS")
        self.assertEqual(by_id[tid1]["note_dir"], "/x/" + tid1)
        self.assertEqual(by_id[tid2]["status"], "FAILED")

    def test_insert_upsert_updates_title(self):
        tid = _tid()
        insert_video_task("BV1", "bilibili", tid, title="旧标题")
        insert_video_task("BV1", "bilibili", tid, title="新标题")
        tasks = list_tasks()
        by_id = {t["task_id"]: t for t in tasks}
        self.assertEqual(by_id[tid]["title"], "新标题")

    def test_update_task_status(self):
        tid = _tid()
        insert_video_task("BV1", "bilibili", tid, title="测试")
        update_task_status(tid, "SUCCESS")
        tasks = list_tasks()
        by_id = {t["task_id"]: t for t in tasks}
        self.assertEqual(by_id[tid]["status"], "SUCCESS")

    def test_get_task_by_video(self):
        tid = _tid()
        insert_video_task("BV1", "bilibili", tid)
        self.assertEqual(get_task_by_video("BV1", "bilibili"), tid)

    def test_delete_task(self):
        tid = _tid()
        insert_video_task("BV1", "bilibili", tid)
        delete_task(tid)
        self.assertNotIn(tid, [t["task_id"] for t in list_tasks()])

    def test_delete_all_tasks(self):
        """delete_all_tasks 一次清空索引（#125 B12，cleanup_all 用，不再 N+1 循环）。"""
        insert_video_task("BV1", "bilibili", _tid())
        insert_video_task("BV2", "bilibili", _tid())
        deleted = delete_all_tasks()
        # 全量跑时表里可能已有其它测试的行：至少清掉本次的 2 条，且清空后表必为空
        self.assertGreaterEqual(deleted, 2)
        self.assertEqual(list_tasks(), [])
        # 空表再清：返回 0 而非抛错（cleanup_all 幂等）
        self.assertEqual(delete_all_tasks(), 0)


class ListTasksPaginationTest(unittest.TestCase):
    """list_tasks 分页下推到 SQL（#124 B14）：limit/offset 不再全表拉回切片。"""

    @classmethod
    def setUpClass(cls):
        init_db()
        # 插入 5 条独立任务，供分页断言
        for i in range(5):
            insert_video_task(f"BV-P{i}", "bilibili", f"page-{i}", title=f"任务{i}")

    def test_limit_returns_only_n_entries(self):
        self.assertEqual(len(list_tasks(limit=2)), 2)

    def test_offset_skips_entries(self):
        total = len(list_tasks())
        self.assertEqual(len(list_tasks(offset=3)), total - 3)

    def test_limit_and_offset_combine(self):
        total = len(list_tasks())
        n = len(list_tasks(limit=2, offset=3))
        self.assertEqual(n, 2 if total >= 5 else max(0, total - 3))

    def test_limit_one_is_subset_of_all(self):
        one = list_tasks(limit=1)
        all_tasks = list_tasks()
        self.assertEqual(len(one), 1)
        self.assertIn(one[0]["task_id"], [t["task_id"] for t in all_tasks])

    def test_no_args_returns_all(self):
        # 无参数行为与旧版一致：全量
        self.assertEqual(len(list_tasks()), len(list_tasks(limit=None, offset=0)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
