"""Installer operations executed inside the exact environment registered for MCP."""
import argparse
import json
import sys
import tempfile
import wave
from pathlib import Path

PREFIX = "VIDEONOTE_INSTALL_RESULT="


def probe() -> dict:
    import importlib.util

    from videonote_mcp import __version__
    from videonote_mcp.hardware import inspect_hardware

    hardware = inspect_hardware()
    return {"version": __version__, "cuda": hardware.cuda_available,
            "mlx": hardware.mlx_supported and importlib.util.find_spec("mlx_whisper") is not None,
            "detail": hardware.detail}


def prepare(engine: str, size: str, device: str, download: bool, verify: bool,
            expected_profile_hash: str | None = None) -> dict:
    from videonote_mcp.config import setup_environment

    setup_environment()
    from app.services.transcriber_config_manager import TranscriberConfigManager
    from app.utils.model_status import (
        check_mlx_whisper_model_exists,
        check_whisper_model_exists,
    )

    ready = False
    local = engine in ("fast-whisper", "mlx-whisper")
    if local:
        check = check_mlx_whisper_model_exists if engine == "mlx-whisper" else check_whisper_model_exists
        ready = check(size)
        if download and not ready:
            from videonote_mcp.cli import _download_mlx_model, _download_whisper

            (_download_mlx_model if engine == "mlx-whisper" else _download_whisper)(size)
            ready = check(size)
            if not ready:
                raise RuntimeError("模型下载后仍未就绪")
        if ready and verify:
            verify_asr(engine, size, device)
    # Failed downloads/inference must not replace a working transcription profile.
    manager = TranscriberConfigManager()
    # Compare persisted fields, not environment-backed defaults: terminal env
    # variables are not necessarily inherited by a GUI MCP client.
    if expected_profile_hash is not None:
        from videonote_mcp.installer_profiles import profile_digest, read_settings

        with manager._lock:
            # Read strictly without the manager's corrupt-file recovery. Keep
            # this snapshot through the write instead of reading it again.
            stored = read_settings(manager.path.parent)
            if profile_digest(stored) != expected_profile_hash:
                print("转写配置在选择后发生变化；保留现有文件，请重新运行安装向导。", file=sys.stderr)
                raise RuntimeError("Transcription profile changed during installation")
            if stored.get("transcriber_type") != engine or stored.get("whisper_model_size") != size:
                manager._write({**stored, "transcriber_type": engine, "whisper_model_size": size})
    else:
        stored = manager._read()
        if stored.get("transcriber_type") != engine or stored.get("whisper_model_size") != size:
            manager.update_config(engine, size)
    return {"model_ready": ready if local else None, "asr_verified": local and ready and verify}


def verify_asr(engine: str, size: str, device: str) -> None:
    with tempfile.TemporaryDirectory(prefix="videonote-asr-") as directory:
        audio = Path(directory) / "silence.wav"
        with wave.open(str(audio), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 16000)
        if engine == "mlx-whisper":
            from app.transcriber.mlx_whisper_transcriber import MLXWhisperTranscriber

            transcriber = MLXWhisperTranscriber(model_size=size)
        else:
            from app.transcriber.whisper import WhisperTranscriber

            transcriber = WhisperTranscriber(model_size=size, device=device)
            if device == "cuda" and transcriber.device != "cuda":
                raise RuntimeError("CUDA 验证回落到了 CPU")
        if engine == "mlx-whisper":
            transcriber.transcript(str(audio))
        else:
            # Disable VAD: otherwise silence could be discarded before any GPU
            # computation. Iterating the lazy generator actually runs inference.
            segments, _ = transcriber.model.transcribe(str(audio), language="en", vad_filter=False, beam_size=1)
            list(segments)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("probe", "prepare"))
    parser.add_argument("--engine", default="fast-whisper")
    parser.add_argument("--size", default="small")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--no-verify", action="store_true")
    parser.add_argument("--expected-profile-hash")
    options = parser.parse_args(argv)
    try:
        result = probe() if options.action == "probe" else prepare(
            options.engine, options.size, options.device, options.download, not options.no_verify,
            options.expected_profile_hash)
    except Exception as exc:
        # Native/network errors may contain signed URLs. Report only the type;
        # downloader and transcriber logs contain the detailed diagnostics.
        print(f"安装环境检查失败：{type(exc).__name__}", file=sys.stderr)
        return 1
    print(PREFIX + json.dumps(result), file=sys.stdout)
    return 0
