import os
from abc import ABC

from app.decorators.timeit import timeit
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.services.provider import ProviderService
from app.transcriber.base import Transcriber
from app.utils.logger import get_logger
from app.utils.openai_client import build_openai_client

logger = get_logger(__name__)
import tempfile

import ffmpeg
from dotenv import load_dotenv

if not os.environ.get("VIDEONOTE_DATA_DIR"):
    load_dotenv()
MAX_SIZE_MB = 18
MAX_SIZE_BYTES = MAX_SIZE_MB * 1024 * 1024
def compress_audio(input_path: str, target_bitrate='64k') -> str:
    output_fd, output_path = tempfile.mkstemp(suffix=".mp3")  # 临时输出文件
    os.close(output_fd)  # 关闭文件描述符，ffmpeg 会用路径操作
    try:
        ffmpeg.input(input_path).output(output_path, audio_bitrate=target_bitrate).run(
            quiet=True, overwrite_output=True, timeout=600
        )
    except Exception:
        # mkstemp 已落盘、ffmpeg 失败 → 不清理就残留临时 mp3（调用方拿不到
        # temp_file，finally 无从删起）（#121 B7）
        try:
            os.remove(output_path)
        except OSError:
            pass
        raise
    return output_path

class GroqTranscriber(Transcriber, ABC):


    @timeit
    def transcript(self, file_path: str) -> TranscriptResult:
        file_size = os.path.getsize(file_path)
        temp_file = None  # 压缩产生的临时 mp3，结束后清理
        if file_size > MAX_SIZE_BYTES:
            logger.info(f"文件超过 {MAX_SIZE_MB}MB，开始压缩（当前 {round(file_size / (1024 * 1024), 2)}MB）...")
            file_path = compress_audio(file_path)
            temp_file = file_path
            logger.info(f"压缩完成，临时路径：{file_path}")
        # 按名称查找（#127 B1）：CLI providers add 强制 uuid id，硬编码 id='groq' 只对
        # seed 行生效——按向导新建 groq 得到 uuid id 后引擎永远读空 key 的 seed 行；
        # 库非空时 seed 被跳过 id='groq' 永不出现。名称可配（seed 或用户 add 都叫 Groq）。
        provider = ProviderService.get_provider_by_name('groq')
        if not provider:
            # 兜底：用户可能改过名称，尝试历史 seed 的固定 id
            provider = ProviderService.get_provider_by_id('groq')

        if not provider:
            raise Exception("Groq 供应商未配置,请配置以后使用。")
        # build_openai_client 会校验 api_key 非空（空 key 会抛天书般的
        # `Illegal header value b'Bearer '`），并自动注入全局代理
        client = build_openai_client(
            api_key=provider.get('api_key'),
            base_url=provider.get('base_url'),
            key_label="Groq 转写引擎的 API Key",
        )
        filename = file_path
        model = os.getenv("GROQ_TRANSCRIBER_MODEL") or "whisper-large-v3"
        try:
            with open(filename, "rb") as file:
                transcription = client.audio.transcriptions.create(
                    file=(filename, file.read()),
                    model=model,
                    response_format="verbose_json",
                )
            segments = []

            for seg in transcription.segments:
                text = seg.text.strip()
                segments.append(TranscriptSegment(
                    start=seg.start,
                    end=seg.end,
                    text=text
                ))

            result = TranscriptResult(
                language=transcription.language,
full_text=" ".join(seg.text for seg in segments).strip(),
                segments=segments,
                raw=transcription.to_dict()
            )
            return result
        finally:
            # 清理压缩临时文件（成功/失败都不残留）
            if temp_file:
                try:
                    os.remove(temp_file)
                except OSError:
                    pass
