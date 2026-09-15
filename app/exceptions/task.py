# 任务取消相关（独立小模块，避免 note -> gpt_factory -> universal_gpt -> note 的循环导入）
import threading
from typing import Optional


class TaskCancelledError(Exception):
    """任务被取消（cancel_note 协作式取消时抛出）。"""


class OfficialTranscriptFetchError(RuntimeError):
    """已登录平台的官方文稿请求失败，不应静默回退 ASR（#144 B1）。"""


class TranscriberRetiredError(RuntimeError):
    """Whisper 实例已因尺寸切换退役，且当前没有**同尺寸**的受管实例可安全转交。

    退役实例既不能按旧尺寸自愈重建（会生成脱离 provider 注册表、不受空闲回收管理
    的「游离模型」），也不能静默改用不同尺寸的注册实例（会把本任务结果挂到旧尺寸的
    缓存键上，污染后续缓存命中）。调用方（含分块预处理）必须显式透传本异常，让任务
    进入失败/重试路径 —— 不能被「单块失败跳过」的通用容错吞掉后把缺块结果写进缓存。

    放在本轻量模块而非 whisper.py：分块流水线需要捕获它，但不应为此提前加载整个
    faster-whisper 引擎。
    """


def check_cancel(cancel_event: Optional[threading.Event]) -> None:
    """协作式取消检查：cancel_event 已 set 则抛 TaskCancelledError（在阶段边界调用）。"""
    if cancel_event is not None and cancel_event.is_set():
        raise TaskCancelledError("任务已取消")
