"""多文件音频合并 —— FFmpeg concat。

把多个音频/视频文件（会议分段、多段录音、多个本地视频）合并成单个音频文件，
供后续转写/总结。为保证兼容性（来源编码可能不同：mp3/m4a/wav/opus），
先逐个统一转成 16kHz mono WAV，再用 concat demuxer 无损拼接。
"""
import os
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Union

from app.utils.logger import get_logger
from app.utils.path_helper import get_data_dir

logger = get_logger(__name__)

# ffmpeg 单步超时（秒）：损坏文件/管道阻塞时避免 worker 线程永久挂死
#（挂起的 subprocess 会占住 3-worker 池，导致后续所有任务排队不动）
_FFMPEG_TIMEOUT = 1800

# 输出缺省落数据目录（与 pipeline/note 同源，#129 B3）：裸脚本调 merge_audio() 产物
# 不再落不确定的 CWD——曾缺省 Path.cwd()，MCP 启动目录/临时目录都出现过，难找且对清理失明
NOTE_OUTPUT_DIR = Path(os.getenv("NOTE_OUTPUT_DIR", str(Path(get_data_dir()) / "note_results")))


def _to_wav(input_path: str, out_path: str) -> None:
    """ffmpeg 转 16kHz mono wav。失败抛 RuntimeError。"""
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vn", "-ar", "16000", "-ac", "1",
        "-c:a", "pcm_s16le", out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=_FFMPEG_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 转换失败 {input_path}: {r.stderr.decode('utf-8', 'replace')[-300:]}"
        )


def merge_audio(
    files: List[Union[str, Path]],
    out_dir: Optional[Union[str, Path]] = None,
    out_name: str = "merged",
) -> str:
    """把多个音频/视频文件合并为一个 16kHz mono wav，返回输出路径。

    - files: 至少 2 个本地文件路径（音频或视频皆可）；
    - out_dir: 输出目录（缺省数据目录 note_results/merged/）；out_name: 输出文件名（不含扩展名）。
    返回 f"{out_dir}/{out_name}.wav"。中途失败清理临时文件。
    """
    if not files or len(files) < 2:
        raise ValueError(f"至少需要 2 个文件，收到 {len(files)} 个")
    paths = [str(Path(f).expanduser()) for f in files]
    for p in paths:
        if not os.path.exists(p):
            raise FileNotFoundError(f"文件不存在: {p}")

    if out_dir:
        out_dir = Path(out_dir).expanduser()
    else:
        # 缺省数据目录（与 server 层 merge_audio 的 NOTE_OUTPUT_DIR/merged 同口径）
        out_dir = NOTE_OUTPUT_DIR / "merged"
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / f"{out_name}.wav"

    tmpdir = Path(tempfile.mkdtemp(prefix="videonote_merge_"))
    try:
        # 1) 统一转 16kHz mono wav
        wavs: List[str] = []
        for i, p in enumerate(paths):
            w = tmpdir / f"part_{i:03d}.wav"
            _to_wav(p, str(w))
            wavs.append(str(w))
        # 2) concat demuxer：list 文件按顺序列出，`-c copy` 拼接（同格式 wav）
        list_file = tmpdir / "concat.txt"
        list_file.write_text(
            "".join(f"file '{w}'\n" for w in wavs), encoding="utf-8"
        )
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file), "-c", "copy", str(final_path),
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=_FFMPEG_TIMEOUT)
        if r.returncode != 0:
            raise RuntimeError(
                f"ffmpeg concat 失败: {r.stderr.decode('utf-8', 'replace')[-300:]}"
            )
        return str(final_path)
    finally:
        # 清理临时目录（转换中间 wav + list 文件）
        try:
            for f in tmpdir.iterdir():
                f.unlink(missing_ok=True)
            tmpdir.rmdir()
        except OSError:
            pass
