from app.gpt.base import GPT
from app.gpt.prompt_builder import generate_base_prompt
from app.models.gpt_model import GPTSource
from app.exceptions.task import check_cancel as _check_cancel
import os
import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from app.gpt.prompt import MERGE_PROMPT
from app.gpt.request_chunker import RequestChunker
from app.models.transcriber_model import TranscriptSegment
from typing import List, Optional
import re

# 大纲注入上限（docs/05 #39 标题漂移）：标题数量与单条长度都截断，
# 控制注入体积（估算切块时不带大纲，留 ~5% 余量即可容纳）
_OUTLINE_MAX_ITEMS = 15
_OUTLINE_MAX_TITLE_LEN = 40


def extract_outline(partials: List[str], limit: int = _OUTLINE_MAX_ITEMS,
                    max_title_len: int = _OUTLINE_MAX_TITLE_LEN) -> str:
    """从已生成的笔记片段中提取章节标题，供后续 chunk 沿用统一标题风格。

    - 只取 `#`~`####` 标题行；去掉行内 markdown 记号（加粗/反引号）与
      `*Content-[mm:ss]` 时间戳后缀
    - 按出现顺序去重（同名/同义标题只保留第一个）
    - 超出 limit 截断（防止注入体积失控）
    """
    titles: List[str] = []
    seen = set()
    for partial in partials or []:
        for line in re.findall(r"^#{1,4}\s+(.+)$", partial, re.M):
            title = re.sub(r"\s*\*Content-\[[^\]]*\]\s*$", "", line)
            title = title.replace("**", "").replace("`", "").replace("*", "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            titles.append(title[:max_title_len])
            if len(titles) >= limit:
                return "\n".join(f"- {t}" for t in titles)
    return "\n".join(f"- {t}" for t in titles)


class UniversalGPT(GPT):
    def __init__(self, client, model: str, temperature: float = 0.7):
        self.client = client
        self.model = model
        self.temperature = temperature
        self.screenshot = False
        self.link = False
        self.max_request_bytes = int(os.getenv("OPENAI_MAX_REQUEST_BYTES", str(45 * 1024 * 1024)))
        # token 级切块上限（docs/05 #32）：按窗口切，而不是 45MB 字节一整块。
        # 汉字≈1 token 的保守估计，默认 12000 留足输出余量（8-16k 窗口兼容）。
        self.max_tokens_per_chunk = int(os.getenv("OPENAI_MAX_TOKENS_PER_CHUNK", "12000"))
        self.checkpoint_dir = Path(os.getenv("NOTE_OUTPUT_DIR", "note_results"))
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        # 初始化时缓存重试配置，避免每次请求重复读取环境变量
        self._max_retry_attempts = max(1, int(os.getenv("OPENAI_RETRY_ATTEMPTS", "3")))
        self._retry_base_backoff = float(os.getenv("OPENAI_RETRY_BACKOFF_SECONDS", "1.5"))

    def _format_time(self, seconds: float) -> str:
        # ≥1h 保留小时位（00:00 的旧实现会截断），<1h 输出 MM:SS
        total = int(seconds)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _build_segment_text(self, segments: List[TranscriptSegment]) -> str:
        # 说话人标签只在确实有 2+ 位时渲染（docs/05 #31）：单人视频的
        # 整片 SPEAKER_00 前缀是无信息噪音。
        speakers = {getattr(seg, "speaker", None) for seg in segments} - {None}
        show_speaker = len(speakers) > 1
        lines = []
        for seg in segments:
            speaker = getattr(seg, "speaker", None) if show_speaker else None
            prefix = f"[{speaker}] " if speaker else ""
            lines.append(f"{self._format_time(seg.start)} - {prefix}{seg.text.strip()}")
        return "\n".join(lines)

    def ensure_segments_type(self, segments) -> List[TranscriptSegment]:
        return [TranscriptSegment(**seg) if isinstance(seg, dict) else seg for seg in segments]

    def create_messages(self, segments: List[TranscriptSegment], **kwargs):

        comments_danmaku = kwargs.get('comments_danmaku')

        content_text = generate_base_prompt(
            title=kwargs.get('title'),
            segment_text=self._build_segment_text(segments),
            tags=kwargs.get('tags'),
            _format=kwargs.get('_format'),
            style=kwargs.get('style'),
            extras=kwargs.get('extras'),
            comments_danmaku=comments_danmaku,
            outline=kwargs.get('outline'),
        )

        video_img_urls = kwargs.get('video_img_urls', [])

        content: list[dict] | str
        if video_img_urls:
            # 有截图时走 OpenAI 多模态 content 数组（text + image_url）
            content = [{"type": "text", "text": content_text}]
            for url in video_img_urls:
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": url,
                        "detail": "auto"
                    }
                })
        else:
            # 纯文本场景退回 string content：DeepSeek deepseek-chat 等非多模态模型
            # 不识别 [{"type":"text",...}] 数组形态，会返回 invalid_request_error
            # （issue #282）。OpenAI 规范本身也允许 content 为 string。
            content = content_text

        messages = [{
            "role": "user",
            "content": content
        }]

        return messages

    def list_models(self):
        return self.client.models.list()

    def _estimate_messages_bytes(self, messages: list) -> int:
        import json
        return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))

    def _build_merge_messages(self, partials: list) -> list:
        merge_text = MERGE_PROMPT + "\n\n" + "\n\n---\n\n".join(partials)
        # 合并阶段没有图片，直接用 string content 兼容非多模态模型（issue #282）
        return [{
            "role": "user",
            "content": merge_text
        }]

    def _checkpoint_path(self, checkpoint_key: str) -> Path:
        safe_key = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in checkpoint_key)
        # 落盘到任务夹 gen/ 下（而非 note_results 根目录扁平 {task_id}.gpt.checkpoint.json）：
        # 与 note.py:203 的 manifest 记录 gen/checkpoint.json 一致，
        # 取消/失败残留才能被 cleanup_note / cleanup_all 清理到。
        path = self.checkpoint_dir / safe_key / "gen" / "checkpoint.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _build_source_signature(self, source: GPTSource) -> str:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_request_bytes": self.max_request_bytes,
            "max_tokens_per_chunk": self.max_tokens_per_chunk,
            "title": source.title,
            "tags": source.tags,
            "format": source._format,
            "style": source.style,
            "extras": source.extras,
            "video_img_urls": source.video_img_urls or [],
            "comments_danmaku": source.comments_danmaku or "",
            "segments": [
                {
                    "start": getattr(seg, "start", None),
                    "end": getattr(seg, "end", None),
                    "text": getattr(seg, "text", "")
                }
                for seg in source.segment
            ],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _load_checkpoint(self, checkpoint_key: str, source_signature: str) -> dict | None:
        path = self._checkpoint_path(checkpoint_key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("source_signature") != source_signature:
                path.unlink(missing_ok=True)
                return None
            return data
        except Exception:
            path.unlink(missing_ok=True)
            return None

    def _save_checkpoint(self, checkpoint_key: str, source_signature: str, partials: list, phase: str) -> None:
        path = self._checkpoint_path(checkpoint_key)
        data = {
            "version": 1,
            "source_signature": source_signature,
            "phase": phase,
            "partials": partials,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def _clear_checkpoint(self, checkpoint_key: str) -> None:
        self._checkpoint_path(checkpoint_key).unlink(missing_ok=True)

    @staticmethod
    def _is_insufficient_quota_error(exc: Exception) -> bool:
        raw = str(exc)
        return (
            "insufficient_user_quota" in raw
            or "预扣费额度失败" in raw
            or "insufficient quota" in raw.lower()
        )

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        raw = str(exc).lower()
        retryable_tokens = (
            "error code: 524",
            "bad_response_status_code",
            "timed out",
            "timeout",
            "rate limit",
            "error code: 429",
            "error code: 500",
            "error code: 502",
            "error code: 503",
            "error code: 504",
            "apiconnectionerror",
            "connection error",
            "service unavailable",
        )
        if any(token in raw for token in retryable_tokens):
            return True

        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        return status in {408, 409, 429, 500, 502, 503, 504, 524}

    @staticmethod
    def _is_temperature_unsupported_error(exc: Exception) -> bool:
        """OpenAI o1/o3/gpt-5 系列等新模型不接受自定义 temperature，
        只允许默认值 1，传 0.7 会报 `'temperature' does not support 0.7 ...`。"""
        raw = str(exc).lower()
        return "temperature" in raw and (
            "does not support" in raw
            or "unsupported_value" in raw
            or "only the default" in raw
        )

    def _do_create(self, messages: list):
        """单次调用。如果模型拒绝自定义 temperature，就地去掉该参数再试一次
        （不消耗外层的重试次数预算），仍失败则把异常抛给外层重试逻辑。"""
        try:
            return self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
            )
        except Exception as exc:
            if self._is_temperature_unsupported_error(exc):
                print(f"[universal_gpt] 模型 {self.model} 不支持自定义 temperature，改用默认值重试")
                return self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                )
            raise

    def _chat_completion_create(self, messages: list):
        last_exc = None
        for attempt in range(self._max_retry_attempts):
            try:
                return self._do_create(messages)
            except Exception as exc:
                last_exc = exc
                if attempt == self._max_retry_attempts - 1 or not self._is_retryable_error(exc):
                    raise
                sleep_seconds = self._retry_base_backoff * (2 ** attempt)
                time.sleep(sleep_seconds)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("chat completion failed without exception")

    def _merge_partials(self, partials: list, checkpoint_key: str | None, source_signature: str | None) -> str:
        def build_messages(texts, *_args, **_kwargs):
            return self._build_merge_messages(texts)

        merge_chunker = RequestChunker(
            lambda *_args, **_kwargs: [],
            self.max_request_bytes,
            self._estimate_messages_bytes,
            max_tokens=self.max_tokens_per_chunk,
        )

        current_partials = list(partials)
        while len(current_partials) > 1:
            groups = merge_chunker.group_texts_by_budget(current_partials, build_messages)
            new_partials = []
            for group_idx, group in enumerate(groups):
                messages = build_messages(group)
                try:
                    response = self._chat_completion_create(messages)
                except Exception:
                    if checkpoint_key and source_signature:
                        self._save_checkpoint(checkpoint_key, source_signature, current_partials, "merge")
                    raise

                content = (response.choices[0].message.content or "").strip()
                if not content:
                    raise ValueError(
                        "模型返回空内容（可能被拒绝/内容过滤），finish_reason="
                        + str(getattr(response.choices[0], "finish_reason", "?"))
                    )
                new_partials.append(content)

                if checkpoint_key and source_signature:
                    remaining_partials = []
                    for remaining_group in groups[group_idx + 1:]:
                        remaining_partials.extend(remaining_group)
                    resumable_partials = new_partials + remaining_partials
                    self._save_checkpoint(checkpoint_key, source_signature, resumable_partials, "merge")

            current_partials = new_partials

        return current_partials[0]

    def summarize(self, source: GPTSource, cancel_event: Optional[threading.Event] = None) -> str:
        self.screenshot = source.screenshot
        self.link = source.link
        source.segment = self.ensure_segments_type(source.segment)
        checkpoint_key = source.checkpoint_key
        source_signature = self._build_source_signature(source) if checkpoint_key else None

        def message_builder(segments, image_urls, **kwargs):
            return self.create_messages(segments, video_img_urls=image_urls, **kwargs)

        chunker = RequestChunker(
            message_builder,
            self.max_request_bytes,
            self._estimate_messages_bytes,
            max_tokens=self.max_tokens_per_chunk,
        )

        # 评论/弹幕只在第一个 chunk 携带一次；传入 chunker 仅用于准确估算首 chunk 体积
        comments_danmaku = getattr(source, "comments_danmaku", None)

        try:
            chunks = chunker.chunk(
                source.segment,
                source.video_img_urls or [],
                title=source.title,
                tags=source.tags,
                _format=source._format,
                style=source.style,
                extras=source.extras,
                comments_danmaku=comments_danmaku,
            )
        except ValueError:
            chunks = chunker.chunk(
                source.segment,
                [],
                title=source.title,
                tags=source.tags,
                _format=source._format,
                style=source.style,
                extras=source.extras,
                comments_danmaku=comments_danmaku,
            )

        partials = []
        if checkpoint_key and source_signature:
            checkpoint = self._load_checkpoint(checkpoint_key, source_signature)
            if checkpoint and isinstance(checkpoint.get("partials"), list):
                partials = checkpoint["partials"]

        if len(partials) > len(chunks):
            partials = []

        # 空素材（无转写分段 + 无帧）时 chunker 返回 []，直接给用户明确错误，
        # 而不是在 _merge_partials([])[0] 抛 IndexError
        if not chunks and not partials:
            raise ValueError(
                "素材为空（无转写分段、无帧图片），无法总结——请先提供转写"
                "（transcribe_media / fetch_subtitles / prepare_note_material）或帧素材"
            )

        for offset, chunk in enumerate(chunks[len(partials):]):
            _check_cancel(cancel_event)  # 每 chunk 前检查取消（LLM 循环内灵敏取消）
            # 评论/弹幕只出现在第一个 chunk（尚未生成任何 partial 时），
            # 其余 chunk 传 None，避免大数据在多个 chunk 重复而爆 token
            chunk_comments = comments_danmaku if (len(partials) == 0 and offset == 0) else None
            # 已有 partial（含 checkpoint 恢复）的章节标题注入后续 chunk，
            # 统一标题风格避免漂移（docs/05 #39）；首个 chunk 无大纲。
            outline = extract_outline(partials)
            messages = self.create_messages(
                chunk.segments,
                title=source.title,
                tags=source.tags,
                video_img_urls=chunk.image_urls,
                _format=source._format,
                style=source.style,
                extras=source.extras,
                comments_danmaku=chunk_comments,
                outline=outline,
            )
            try:
                response = self._chat_completion_create(messages)
            except Exception:
                if checkpoint_key and source_signature:
                    self._save_checkpoint(checkpoint_key, source_signature, partials, "summarize")
                raise

            content = (response.choices[0].message.content or "").strip()
            if not content:
                raise ValueError(
                    "模型返回空内容（可能被拒绝/内容过滤），finish_reason="
                    + str(getattr(response.choices[0], "finish_reason", "?"))
                )
            partials.append(content)
            if checkpoint_key and source_signature:
                self._save_checkpoint(checkpoint_key, source_signature, partials, "summarize")

        if len(partials) == 1:
            if checkpoint_key:
                self._clear_checkpoint(checkpoint_key)
            return partials[0]
        merged = self._merge_partials(partials, checkpoint_key, source_signature)
        if checkpoint_key:
            self._clear_checkpoint(checkpoint_key)
        return merged
