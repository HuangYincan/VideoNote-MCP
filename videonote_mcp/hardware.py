"""Bounded hardware probes; importing this module never initializes VideoNote."""
import json
import platform
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Hardware:
    platform: str
    machine: str
    mlx_supported: bool
    cuda_available: bool
    nvidia_present: bool
    detail: str

    @property
    def recommended(self) -> str:
        return "mlx" if self.mlx_supported else "cuda" if self.cuda_available else "cpu"


def mlx_supported(system: str, machine: str, mac_version: str) -> bool:
    try:
        major = int(mac_version.split(".")[0])
    except ValueError:
        return False
    return system == "darwin" and machine.lower() in ("arm64", "aarch64") and major >= 14


def inspect_hardware() -> Hardware:
    system, machine = sys.platform, platform.machine()
    if mlx_supported(system, machine, platform.mac_ver()[0]):
        return Hardware(system, machine, True, False, False, "Apple Silicon / native ARM Python / macOS 14+")
    # Native imports can hang on some Windows machines. Keep them in a child
    # and use CTranslate2's own backend, rather than installing/importing torch.
    script = (
        "import json; import ctranslate2; "
        "from app.utils.env_checker import is_ct2_cuda_available; "
        "print(json.dumps({'nvidia': ctranslate2.get_cuda_device_count() > 0, "
        "'cuda': is_ct2_cuda_available()}))"
    )
    cuda = nvidia = False
    detail = "未检测到可用 CUDA；推荐 CPU"
    try:
        probe = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=15)
        if probe.returncode == 0:
            data = json.loads(probe.stdout.strip().splitlines()[-1])
            cuda, nvidia = bool(data["cuda"]), bool(data["nvidia"])
            if cuda:
                detail = "CTranslate2 检测到 NVIDIA GPU 和匹配的 CUDA 运行库"
            elif nvidia:
                detail = "检测到 NVIDIA GPU，但所需 CUDA 运行库不可用；推荐 CPU"
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, IndexError):
        detail = "CUDA 检测未完成；推荐 CPU，可手动选择后重新验证"
    if not nvidia and system in ("linux", "win32"):
        try:
            result = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=5)
            nvidia = result.returncode == 0 and bool(result.stdout.strip())
            if nvidia:
                detail = "检测到 NVIDIA GPU，但当前环境无法确认 CUDA 推理可用；推荐 CPU"
        except (OSError, subprocess.TimeoutExpired):
            pass
    if system == "darwin":
        detail = "MLX 需要 macOS 14+ 和原生 Apple Silicon Python；当前环境推荐 CPU"
    return Hardware(system, machine, False, cuda, nvidia, detail)
