import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from app.utils.path_helper import get_data_dir

if not os.environ.get("VIDEONOTE_DATA_DIR"):
    load_dotenv()

# 默认 SQLite，如果想换 PostgreSQL 或 MySQL，可以直接改 .env。
# 默认路径固定到稳定数据目录（与 videonote_mcp.config 的 setdefault 同值）——
# 相对路径 `sqlite:///video_note.db` 会随进程 CWD 漂移，在仓库根/其它目录
# 跑脚本或测试会分裂出多个互不相通的 DB（本仓库根目录曾泄漏出 video_note.db）。
DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{os.path.join(get_data_dir(), 'video_note.db')}"

# SQLite 需要特定连接参数，其他数据库不需要
engine_args = {}
if DATABASE_URL.startswith("sqlite"):
    engine_args["connect_args"] = {
        "check_same_thread": False,
        # 连接级 busy timeout（秒）：MCP 多线程并发写（任务索引/provider 更新）
        # 时避免立刻抛 "database is locked"
        "timeout": 30,
    }

_pool_args = {}
if not DATABASE_URL.startswith("sqlite"):
    _pool_args = {
        "pool_size": int(os.getenv("DB_POOL_SIZE", "10")),
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "20")),
        "pool_pre_ping": True,
    }

engine = create_engine(
    DATABASE_URL,
    echo=os.getenv("SQLALCHEMY_ECHO", "false").lower() == "true",
    **engine_args,
    **_pool_args,
)

if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        """SQLite 并发写优化：WAL + busy_timeout + NORMAL 同步。

        否则两个线程并发写会互锁抛 OperationalError: database is locked；
        WAL 让读写不互斥，busy_timeout 让短竞争自旋而非立刻失败。
        """
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_engine():
    return engine


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()