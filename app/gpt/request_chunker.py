from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass
class ChunkPayload:
    segments: list
    image_urls: list


class RequestChunker:
    def __init__(
        self,
        message_builder: Callable,
        max_bytes: int,
        size_estimator: Optional[Callable] = None,
        max_tokens: Optional[int] = None,
        token_estimator: Optional[Callable] = None,
    ):
        self.message_builder = message_builder
        self.max_bytes = max_bytes
        self.size_estimator = size_estimator
        # token 级上限（docs/05 #32）：max_tokens=None 时保持纯字节行为
        self.max_tokens = max_tokens
        self.token_estimator = token_estimator

    def estimate(self, messages) -> int:
        if self.size_estimator:
            return self.size_estimator(messages)
        import json
        return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))

    def _messages_size(self, segments, image_urls, **kwargs) -> int:
        messages = self.message_builder(segments, image_urls, **kwargs)
        return self.estimate(messages)

    def _estimate_tokens(self, messages) -> int:
        """消息 token 近似：汉字≈1 token，其余字符≈4字符/token。

        不引入 tiktoken 重依赖；对中文场景偏保守（不低估），英文按 4 字符/token。
        """
        if self.token_estimator:
            return self.token_estimator(messages)
        import json

        text = json.dumps(messages, ensure_ascii=False)
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        return cjk + (len(text) - cjk) // 4

    def _fits(self, segments, image_urls, **kwargs) -> bool:
        """字节与 token 双约束（message_builder 只构建一次）。"""
        messages = self.message_builder(segments, image_urls, **kwargs)
        if self.estimate(messages) > self.max_bytes:
            return False
        if self.max_tokens and self._estimate_tokens(messages) > self.max_tokens:
            return False
        return True

    def _get_text(self, segment) -> str:
        if isinstance(segment, dict):
            return segment.get("text", "")
        return getattr(segment, "text", "")

    def _make_segment(self, segment, text: str):
        if isinstance(segment, dict):
            new_seg = dict(segment)
            new_seg["text"] = text
            return new_seg
        if hasattr(segment, "__dict__"):
            data = dict(segment.__dict__)
            data["text"] = text
            return type(segment)(**data)
        return type(segment)(segment.start, segment.end, text)

    def _split_segment_to_fit(self, segment, **kwargs):
        text = self._get_text(segment)
        if not text:
            raise ValueError("empty segment cannot be split")
        lo, hi = 1, len(text)
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = self._make_segment(segment, text[:mid])
            if self._fits([candidate], [], **kwargs):
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if best is None:
            raise ValueError("single segment too large to fit request")
        head = self._make_segment(segment, text[:best])
        tail = self._make_segment(segment, text[best:])
        return head, tail

    def chunk(self, segments: list, image_urls: list, **kwargs) -> List[ChunkPayload]:
        segments = list(segments or [])
        image_urls = list(image_urls or [])
        if not segments and not image_urls:
            return []

        chunks: List[ChunkPayload] = []
        seg_idx = 0

        while seg_idx < len(segments):
            batch_segments = []
            while seg_idx < len(segments):
                candidate = batch_segments + [segments[seg_idx]]
                if self._fits(candidate, [], **kwargs):
                    batch_segments = candidate
                    seg_idx += 1
                    continue
                if not batch_segments:
                    head, tail = self._split_segment_to_fit(segments[seg_idx], **kwargs)
                    segments[seg_idx] = head
                    segments.insert(seg_idx + 1, tail)
                    continue
                break

            if not batch_segments:
                raise ValueError("unable to fit any content into chunk")

            chunks.append(ChunkPayload(segments=batch_segments, image_urls=[]))

        if not image_urls:
            return chunks

        if not chunks:
            chunks = [ChunkPayload(segments=[], image_urls=[])]

        if not segments:
            for image in image_urls:
                appended = False
                for chunk in chunks[-1:]:
                    candidate_images = chunk.image_urls + [image]
                    if self._fits(chunk.segments, candidate_images, **kwargs):
                        chunk.image_urls = candidate_images
                        appended = True
                        break

                if appended:
                    continue

                if not self._fits([], [image], **kwargs):
                    raise ValueError("single image payload exceeds max_bytes")
                chunks.append(ChunkPayload(segments=[], image_urls=[image]))
            return chunks

        chunk_count = len(chunks)
        total_images = len(image_urls)
        for idx, image in enumerate(image_urls):
            preferred_idx = min(chunk_count - 1, (idx * chunk_count) // total_images)
            placed = False

            for chunk_idx in range(preferred_idx, len(chunks)):
                chunk = chunks[chunk_idx]
                candidate_images = chunk.image_urls + [image]
                if self._fits(chunk.segments, candidate_images, **kwargs):
                    chunk.image_urls = candidate_images
                    placed = True
                    break

            if placed:
                continue

            if not self._fits([], [image], **kwargs):
                raise ValueError("single image payload exceeds max_bytes")
            chunks.append(ChunkPayload(segments=[], image_urls=[image]))

        return chunks

    def group_texts_by_budget(self, texts: List[str], build_messages: Callable, **kwargs) -> List[List[str]]:
        groups: List[List[str]] = []
        idx = 0
        while idx < len(texts):
            group: List[str] = []
            while idx < len(texts):
                candidate = group + [texts[idx]]
                try:
                    messages = build_messages(candidate, [], **kwargs)
                except TypeError:
                    messages = build_messages(candidate, **kwargs)
                if self.estimate(messages) <= self.max_bytes and (
                    not self.max_tokens or self._estimate_tokens(messages) <= self.max_tokens
                ):
                    group = candidate
                    idx += 1
                    continue
                if not group:
                    raise ValueError("single text block exceeds max_bytes")
                break
            groups.append(group)
        return groups
