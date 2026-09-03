"""whisper / mlx 模型下载就绪检查（纯函数，无 FastAPI 依赖）。

上游把模型就绪判断放在 app/routers/config.py（依赖 FastAPI），本仓库剥离 Web 层后
把这两个检查函数 + 下载状态查询抽到这里，供 TranscriberConfigManager 做
「开始转写前确认本地模型已下载」的门禁，也供 MCP 的 health_check / 模型管理工具复用。
"""
import logging
import os
from pathlib import Path
from typing import Dict

from app.transcriber import model_download_state as dl_state
from app.utils.path_helper import get_model_dir

logger = logging.getLogger(__name__)


def check_whisper_model_exists(model_size: str, subdir: str = "whisper") -> bool:
    """检查指定 fast-whisper 模型是否已下载完整到本地。"""
    from app.transcriber.whisper_models import (
        hf_cache_dirname,
        is_local_target,
        resolve_whisper_model,
    )
    try:
        target = resolve_whisper_model(model_size)
    except ValueError as exc:
        # 模型名无法解析（自定义映射里登记了坏 target / size 拼写错）——
        # 不区分就返回 False 会被门禁谎报成「模型未下载」让用户反复重下
        logger.warning("模型尺寸 %r 无法解析（这不是下载问题）: %s", model_size, exc)
        return False
    except Exception as exc:  # noqa: BLE001 —— 解析失败按未就绪处理，但必须留痕
        logger.warning("解析模型尺寸 %r 失败: %s", model_size, exc)
        return False
    if is_local_target(target):
        return (Path(target) / "model.bin").exists()

    model_dir = Path(get_model_dir(subdir))
    hf_repo_dir = model_dir / hf_cache_dirname(target) / "snapshots"
    if hf_repo_dir.exists():
        for snapshot in hf_repo_dir.iterdir():
            if (snapshot / "model.bin").exists():
                return True
    legacy = model_dir / f"whisper-{model_size}" / "model.bin"
    return legacy.exists()


# mlx 尺寸 → HF repo_id（与 app/transcriber/mlx_whisper_transcriber.MLX_MODEL_MAP 保持一致）。
# 这里内联是为了避免在向导/健康检查/下载等路径上 import mlx_whisper
# （加载 MLX 框架很重、且其依赖链（numba 等）有循环导入风险）。
MLX_REPO_MAP = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v1": "mlx-community/whisper-large-v1-mlx",
    "large-v2": "mlx-community/whisper-large-v2-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}

# mlx 尺寸 → 固定 revision（main 分支 commit，#142 A2 于 2026-08-25 钉定）。
# snapshot_download 不传 revision 会「要什么拉什么」——上游仓库内容可随时变化，
# 同尺寸模型跨时间下载内核不一致，重装/换机后转写结果漂移；升级模型时与
# MLX_REPO_MAP 一起显式更新（下载完成后本地路径不随上游变化，可离线复用）。
MLX_REPO_REVISIONS: Dict[str, str] = {
    "tiny": "6caf9c55601caafbe6508a8b0d216bdf4783c4e8",
    "base": "1e3e249fb8d01c655324bd6841b1deadffd6d04c",
    "small": "45f3915923c7a79a5a5b5a7d909d39aeb0e5630e",
    "medium": "7fc08c4eac4c316526498f147dfdee6f6303f975",
    "large-v1": "e2cb9fbf9c7aefad760be1dc9b48c075b21288c8",
    "large-v2": "cce86229e2765266197fef869ce9f7e2550067ab",
    "large-v3": "49e6aa286ad60c14352c404340ded53710378a11",
    "large-v3-turbo": "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb",
}


def check_mlx_whisper_model_exists(model_size: str) -> bool:
    """检查指定 mlx-whisper 模型是否已下载完整（以 config.json 为判据）。"""
    repo_id = MLX_REPO_MAP.get(model_size)
    if not repo_id:
        return False
    model_dir = get_model_dir("mlx-whisper")
    model_path = os.path.join(model_dir, repo_id)
    return (Path(model_path) / "config.json").exists()


def is_downloading(key: str) -> bool:
    """该模型是否处于「下载中」状态（进程内内存态）。"""
    return dl_state.get_status(key) == dl_state.DOWNLOADING
