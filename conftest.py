"""pytest 全局夹具：隔离数据目录与 SQLite DB，避免污染真实数据。

问题背景：`app.db.engine` 是模块级单例，`DATABASE_URL` 在首次 import 时读取。
此前每个 DB 测试各自设 `DATABASE_URL` 并直连自己的临时 DB：全量跑套件时，
engine 单例被先收集到的测试文件锁定到某个 DB，后跑的测试直连另一个 DB 而失败
（`test_task_index::MigrationTest::test_init_db_creates_columns` 恒红）。

这里在收集任何测试模块前就把 `DATABASE_URL` 固定到同一个会话级临时 DB，
并用 `NOTE_OUTPUT_DIR` 隔离笔记输出目录（默认指向仓库 note_results/）。
"""
import glob
import os
from pathlib import Path

# 固定 /tmp/videonote_pytest 会被并行 pytest / 多 checkout 撞库（docs/05 #66）：
# 按 pid 隔离，同一进程内所有测试共享，跨进程互不干扰
_TEST_ROOT = Path(f"/tmp/videonote_pytest_{os.getpid()}")
_TEST_ROOT.mkdir(parents=True, exist_ok=True)
_TEST_DB = _TEST_ROOT / "video_note.db"
_NOTE_OUTPUT_DIR = _TEST_ROOT / "note_results"
_NOTE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 每次都从干净库开始（上一个进程若异常退出会残留 -wal/-shm）
for _f in glob.glob(f"{_TEST_DB}*"):
    try:
        os.remove(_f)
    except OSError:
        pass

# 直接赋值（而非 setdefault）：保证先于任何 app.db import 生效，
# 覆盖测试模块里可能残留的旧 DATABASE_URL 写法
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"
os.environ.setdefault("NOTE_OUTPUT_DIR", str(_NOTE_OUTPUT_DIR))
# 数据目录也隔离：server 模块级会 open(DATA_DIR/logs/mcp_stderr.log) 并 dup2(2)，
# 不隔离会把测试 stderr 写进仓库 data/ 并污染 git 工作区（docs 审计 P2-6）
os.environ.setdefault("VIDEONOTE_DATA_DIR", str(_TEST_ROOT / "data"))
