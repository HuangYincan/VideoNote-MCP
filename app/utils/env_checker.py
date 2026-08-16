def is_cuda_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False
def is_torch_installed() -> bool:
    try:
        import torch  # noqa: F401  —— 可用性探测，不需使用
        return True
    except ImportError:
        return False
