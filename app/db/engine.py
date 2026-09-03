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
        # 先收紧文件权限（#140 复扫 A3）：SQLite 默认 0666&~umask 建库（常见 0644）——
        # 同机其他用户可读库中 providers（api_key 已 Fernet 加密，但 base_url 原样明文）。
        # 放在 PRAGMA 前：-wal/-shm 文件复制主库权限（WAL pragma 在其后执行才正确继承）。
        _restrict_sqlite_file_perms()
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        # 外键约束默认是每个 SQLite 连接关闭的；SQLAlchemy 会创建多个
        # Session/连接，必须在 connect listener 中逐连接开启，否则
        # models.provider_id 的外键只停留在 schema 声明层，删除 provider
        # 或插入孤儿 model 时仍可能悄悄破坏一致性。
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


def _restrict_sqlite_file_perms() -> None:
    """把 SQLite 数据文件权限收紧到 0600（失败只忽略——只读介质等场景由连接报错兜底）。

    仅当 DATABASE_URL 是本地 sqlite 路径时生效（:memory: / 远程 URL 跳过）。
    engine.url.database 是 SQLAlchemy 解析好的文件路径（相对/绝对均正确处理，
    无需手工解析 URL——`sqlite:///relative` 不会误拼成根路径）。
    """
    if not DATABASE_URL.startswith("sqlite"):
        return
    db_path = getattr(engine.url, "database", None)
    if not db_path or db_path == ":memory:":
        return
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass

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
