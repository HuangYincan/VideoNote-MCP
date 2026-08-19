"""whisper / mlx 模型后台下载状态跟踪（含失败原因）。

routers.config 的「触发下载」与「查询状态」共享这份进程内内存态：
  - key：fast-whisper 直接用 model_size；mlx 用 "mlx-{size}" 前缀（与历史一致）
  - 状态：downloading / done / failed；failed 时另存最近一次错误原因

为什么抽成独立的轻量模块（仅依赖 logger）：
  1) 把原先散落在 config.py 多处的字符串状态赋值收敛到一处，避免拼写漂移；
  2) 失败原因能透传到 /transcriber_models_status → 前端，修复「下载失败前端无任何
     提示、状态一直显示未下载」（issue #402 的衍生问题：原先状态接口只回传
     downloading/downloaded 两个布尔，failed 态被直接丢弃）；
  3) 不引入 faster_whisper / ctranslate2 等重依赖，可被单测隔离加载。
"""
import threading
from typing import Dict, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

DOWNLOADING = "downloading"
DONE = "done"
FAILED = "failed"

# 并发安全：MCP 每个请求一个线程，两个并发 download_transcriber_model 会同时做
# 检查-标记两步——无锁时双双通过检查、各起一个 worker 重下整个模型（#124 A7）
_lock = threading.Lock()

# key -> 状态字符串；key -> 最近一次失败原因（仅 failed 时有意义）
_status: Dict[str, str] = {}
_errors: Dict[str, str] = {}


def try_mark(key: str) -> bool:
    """原子「未在下载 → 标记为下载中」；返回 True 表示本调用赢得下载权。

    旧 is_downloading + mark_downloading 两步检查有跨线程竞态（调用线程之间），
    MCP 端统一用本原语（#124 A7）。
    """
    with _lock:
        if _status.get(key) == DOWNLOADING:
            return False
        _status[key] = DOWNLOADING
        _errors.pop(key, None)  # 重新开始下载，清掉上一次的失败原因
        return True


def mark_done(key: str) -> None:
    with _lock:
        _status[key] = DONE
        _errors.pop(key, None)


def mark_failed(key: str, error: str = "") -> None:
    with _lock:
        _status[key] = FAILED
        if error:
            _errors[key] = error


def get_status(key: str) -> Optional[str]:
    with _lock:
        return _status.get(key)


def is_downloading(key: str) -> bool:
    with _lock:
        return _status.get(key) == DOWNLOADING


def downloading_keys() -> list:
    """当前所有正在下载的 key（#123 A1：cleanup include_models 前要查这个，防删模型目录打断下载）。"""
    with _lock:
        return [k for k, st in _status.items() if st == DOWNLOADING]


def status_row(name: str, downloaded: bool, key: Optional[str] = None) -> dict:
    """构造单个模型给前端的状态行：downloaded / downloading / failed (+error)。

    key 默认用 name；mlx 传 "mlx-{size}"。已下载成功（downloaded=True）的模型
    一律不回传 failed/error——避免「先失败后又下好」时残留旧的错误状态。
    """
    k = key if key is not None else name
    with _lock:
        st = _status.get(k)
        err = _errors.get(k) if st == FAILED else None
    row: dict = {
        "model_size": name,
        "downloaded": downloaded,
        "downloading": st == DOWNLOADING,
        "failed": (not downloaded) and st == FAILED,
    }
    if row["failed"] and err:
        row["error"] = err
    return row
