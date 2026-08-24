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

import pytest

# 固定 /tmp/videonote_pytest 会被并行 pytest / 多 checkout 撞库（docs/05 #66）：
# 按 pid 隔离，同一进程内所有测试共享，跨进程互不干扰
_TEST_ROOT = Path(f"/tmp/videonote_pytest_{os.getpid()}")
_TEST_ROOT.mkdir(parents=True, exist_ok=True)
_TEST_DB = _TEST_ROOT / "video_note.db"
# 数据目录与 NOTE_OUTPUT_DIR 同生产布局（config.setup_environment：note_results 在
# 数据根内）——清理越界检查（#140 复扫 A1）按「数据根内才清」判定，
# 兄弟目录布局会把所有测试的 note_results 误判为越界
_TEST_DATA_DIR = _TEST_ROOT / "data"
_TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
_NOTE_OUTPUT_DIR = _TEST_DATA_DIR / "note_results"
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
os.environ.setdefault("VIDEONOTE_DATA_DIR", str(_TEST_DATA_DIR))


@pytest.fixture(scope="session", autouse=True)
def _clean_task_registry_at_exit():
    """session 结束时清空 MCP 任务注册表。

    若干契约测试 stub 掉 _pool.submit 后不 pop _task_futures/_task_events，
    进程退出的 atexit 摘要会把这些 mock Future 误报成「进行中/排队任务 N 个」。
    """
    yield
    import videonote_mcp.server as server

    with server._tasks_lock:
        server._task_futures.clear()
        server._task_events.clear()


@pytest.fixture(scope="session", autouse=True)
def _mock_public_dns():
    """SSRF 防护（docs/05 第 16 轮 A1）的域名判定依赖真实 DNS。

    测试环境（含沙箱）的 DNS 可能把任意域名解析到保留段（198.18/15、
    fdfe:dcba:: 等），会让 mock 掉 yt-dlp 的下载器测试被 SSRF 检查误拦。
    统一把 getaddrinfo 桩成公网地址：字面 IP 逻辑（ipaddress）不受影响，
    域名判定在测试里确定化；SSRF 防护自身的测试另行显式 patch。
    """
    import socket
    from unittest import mock

    def _public_addrinfo(*_args, **_kwargs):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("8.8.8.8", 0),
            )
        ]

    with mock.patch("socket.getaddrinfo", side_effect=_public_addrinfo):
        yield
