"""音频预处理 —— 转写前的标准化与超长分块。

设计目标：**默认关、零硬依赖**。核心（16kHz 归一 + 超长分块）只用系统 ffmpeg；
降噪（noisereduce）做成可选 extras，未装时静默跳过。

faster-whisper 内部已自带 Silero VAD + 16kHz 重采样，所以本模块主要服务
云端引擎（groq/bcut/kuaishou 有文件大小/时长上限）和后续的说话人分离（pyannote
要求 wav）。启用后统一把输入转成 16kHz mono wav，超长再按固定时长分块。
"""
import logging
import os
import subprocess
from pathlib import Path
from typing import List, Optional, Union

logger = logging.getLogger(__name__)

# 单步超时（秒）：损坏文件/管道阻塞时避免 worker 线程永久挂死
_FFMPEG_TIMEOUT = 1800


def _ffmpeg(args: List[str], desc: str) -> None:
    r = subprocess.run(["ffmpeg", "-y"] + args, capture_output=True, timeout=_FFMPEG_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(
            f"{desc} 失败: {r.stderr.decode('utf-8', 'replace')[-300:]}"
        )


def normalize_to_wav(
    input_path: Union[str, Path],
    out_dir: Optional[Union[str, Path]] = None,
) -> str:
    """把任意音频/视频转为 16kHz mono wav（PCM s16le）。返回输出路径。

    out_dir 缺省：输入文件同目录下 `<原名>_16k.wav`。
    """
    src = str(Path(input_path).expanduser())
    if not os.path.exists(src):
        raise FileNotFoundError(f"文件不存在: {src}")
    out_dir = Path(out_dir).expanduser() if out_dir else Path(src).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    base = Path(src).stem
    out = str(out_dir / f"{base}_16k.wav")
    _ffmpeg(
        ["-i", src, "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", out],
        "转 16kHz mono wav",
    )
    return out


def probe_duration(wav_path: str) -> float:
    """用 ffprobe 取音频时长（秒）。失败返回 0。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", wav_path],
            capture_output=True, text=True, timeout=60,
        )
        return float(r.stdout.strip())
    except Exception as exc:
        # ffprobe 失败静默返回 0 会让 chunk_if_long 把未知时长当「不长」——超长音频
        # 整块喂给云端引擎（可能超限），且无任何留痕（#118）
        logger.warning(f"ffprobe 探测时长失败: {exc}")
        return 0.0


def chunk_if_long(
    wav_path: str,
    max_seconds: int = 1800,
    out_dir: Optional[Union[str, Path]] = None,
) -> List[str]:
    """超长 wav 按固定时长分块（ffmpeg `-f segment`）。返回分块路径列表。

    - 时长 ≤ max_seconds：返回 [wav_path]（不切分）；
    - 超长：按 max_seconds 秒切成多段 `part_000/001/....wav`（编码不变 PCM）。
    """
    duration = probe_duration(wav_path)
    if duration <= 0 or duration <= max_seconds:
        return [wav_path]
    out_dir = Path(out_dir).expanduser() if out_dir else Path(wav_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(out_dir / f"{Path(wav_path).stem}_part_%03d.wav")
    _ffmpeg(
        [
            "-i", wav_path, "-f", "segment",
            "-segment_time", str(int(max_seconds)),
            "-c", "copy", pattern,
        ],
        "超长音频分块",
    )
    chunks = sorted(str(p) for p in Path(out_dir).glob(f"{Path(wav_path).stem}_part_*.wav"))
    return chunks or [wav_path]


def denoise(wav_path: str) -> str:
    """可选降噪（noisereduce）。未安装时返回原路径 + 日志（静默降级）。"""
    try:
        import noisereduce as nr  # noqa: F401
    except ImportError:
        logger.info("noisereduce 未安装，跳过降噪（装 `uvx --with noisereduce` 可启用）")
        return wav_path
    try:
        import numpy as np
        from scipy.io import wavfile

        rate, data = wavfile.read(wav_path)
        if data.dtype != np.int16:
            data = (data * 32767).astype(np.int16)
        if data.ndim == 2:
            data = data.mean(axis=1).astype(np.int16)
        reduced = nr.reduce_noise(y=data.astype(np.float32), sr=rate)
        out = wav_path.replace(".wav", "_denoised.wav")
        wavfile.write(out, rate, reduced.astype(np.int16))
        return out
    except Exception as exc:  # noqa: BLE001 —— 降噪失败不阻断
        logger.warning(f"降噪失败，返回原文件: {exc}")
        return wav_path


def preprocess_pipeline(
    input_path: Union[str, Path],
    max_seconds: int = 1800,
    enable_denoise: bool = False,
    work_dir: Optional[Union[str, Path]] = None,
) -> List[str]:
    """一次性预处理：归一 → （可选降噪）→ 超长分块。

    返回可直接喂给转写引擎的音频文件路径列表（≤ max_seconds 时单元素）。
    归一后文件落在 work_dir（缺省输入同目录）；分块在归一文件旁。
    """
    wav = normalize_to_wav(input_path, out_dir=work_dir)
    if enable_denoise:
        wav = denoise(wav)
    return chunk_if_long(wav, max_seconds=max_seconds, out_dir=work_dir)


def cleanup_preprocess_files(wav_path: str) -> None:
    """清理预处理产生的临时文件（`<名>_16k.wav` / `_part_*.wav` / `_denoised.wav`）。

    这些文件创建在源文件同目录（含用户直接传的文件），转写后不清理会污染目录、
    持续累积（长音频临时 wav 体积大）。只删我们生成的临时文件，绝不碰源文件。
    失败静默。
    """
    p = Path(wav_path)
    parent = p.parent
    # 只删我们生成的临时文件，绝不碰源文件。
    # 正常调用传入 normalize_to_wav 产物（foo_16k.wav）；
    # 误把源路径（foo.wav / foo.mp3）传进来时，只清旁边的 _16k / _part / _denoised。
    candidates = []
    if p.suffix.lower() == ".wav" and p.stem.endswith("_16k"):
        candidates.append(p)
        candidates.extend(parent.glob(f"{p.stem}_part_*.wav"))
        candidates.append(parent / f"{p.stem}_denoised.wav")
    else:
        stem = p.stem
        candidates.append(parent / f"{stem}_16k.wav")
        candidates.extend(parent.glob(f"{stem}_16k_part_*.wav"))
        candidates.append(parent / f"{stem}_16k_denoised.wav")
        candidates.append(parent / f"{stem}_denoised.wav")
    seen = set()
    for f in candidates:
        try:
            fp = Path(f)
            if fp in seen or not fp.is_file():
                continue
            seen.add(fp)
            fp.unlink(missing_ok=True)
        except OSError:
            pass
