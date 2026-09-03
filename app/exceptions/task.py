# 任务取消相关（独立小模块，避免 note -> gpt_factory -> universal_gpt -> note 的循环导入）
import threading
from typing import Optional


class TaskCancelledError(Exception):
    """任务被取消（cancel_note 协作式取消时抛出）。"""


class OfficialTranscriptFetchError(RuntimeError):
    """已登录平台的官方文稿请求失败，不应静默回退 ASR（#144 B1）。"""


def check_cancel(cancel_event: Optional[threading.Event]) -> None:
    """协作式取消检查：cancel_event 已 set 则抛 TaskCancelledError（在阶段边界调用）。"""
    if cancel_event is not None and cancel_event.is_set():
        raise TaskCancelledError("任务已取消")
