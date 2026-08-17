from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass
class ChunkPayload:
    segments: list
    image_urls: list


# 单张图片的固定 token 估算（data URI 按字符计会高估数万 token，见 _estimate_tokens）
_TOKENS_PER_IMAGE = 1105


def _without_comments(kwargs: dict) -> dict:
    """评论/弹幕只注入首 chunk（summarize 循环语义）——估算同样只对首 chunk 计评论，
    否则每个 chunk 的容量预算都按「文本+评论」算，评论大时 chunk 数成倍膨胀（#120）。"""
    if "comments_danmaku" not in kwargs:
        return kwargs
    stripped = dict(kwargs)
    stripped.pop("comments_danmaku", None)
    return stripped


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
        结构感知：`image_url` 的 data URI 是 base64 字节流，按字符计会高估数万
        token（960×540 JPEG ≈ 11 万-34 万字符 → 2.7 万-8.5 万「token」），导致
        视频理解的帧图被全部判超限而静默丢弃 —— 图片按固定 _TOKENS_PER_IMAGE 估算。
        """
        if self.token_estimator:
            return self.token_estimator(messages)
        return self._count_tokens(messages)

    def _count_tokens(self, obj) -> int:
        if isinstance(obj, str):
            cjk = sum(1 for ch in obj if "\u4e00" <= ch <= "\u9fff")
            return cjk + (len(obj) - cjk) // 4
        if isinstance(obj, dict):
            if obj.get("type") == "image_url":
                total = 0
                for key, value in obj.items():
                    # url 是 base64 data URI，固定计 _TOKENS_PER_IMAGE
                    #（OpenAI detail=auto：85 + 170×tiles，960×540≈765，取上限）
                    total += _TOKENS_PER_IMAGE if key == "url" else self._count_tokens(value)
                return total
            return sum(self._count_tokens(v) for v in obj.values())
        if isinstance(obj, (list, tuple)):
            return sum(self._count_tokens(v) for v in obj)
        return 0

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

    def _largest_fitting_prefix(self, segments, image_urls, start: int, **kwargs) -> int:
        """二分找最大的 k：segments[start:start+k] 能装进当前约束。

        fits 单调（多一段只增不减），线性逐段试在长转写（数千段）下每加一段都重建
        整条消息并 json.dumps——O(n²) 累计 CPU（#130 B3）。二分把 _fits 调用从
        O(段数) 降到 O(log 段数)，块边界与线性扫描完全一致（同为最大可容纳前缀）。
        """
        lo, hi = 0, len(segments) - start
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._fits(segments[start:start + mid], image_urls, **kwargs):
                lo = mid
            else:
                hi = mid - 1
        return lo

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
            eff_kwargs = kwargs if not chunks else _without_comments(kwargs)
            # 二分最大可容纳前缀（#130 B3）：等价于线性扫描的停点，但 _fits 调用
            # 数从 O(段数) 降到 O(log 段数)——长转写不再每次重建整条消息 O(n²)
            k = self._largest_fitting_prefix(segments, [], seg_idx, **eff_kwargs)
            if k == 0:
                head, tail = self._split_segment_to_fit(segments[seg_idx], **kwargs)
                segments[seg_idx] = head
                segments.insert(seg_idx + 1, tail)
                continue
            chunks.append(ChunkPayload(segments=segments[seg_idx:seg_idx + k], image_urls=[]))
            seg_idx += k

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
                eff_kwargs = kwargs if chunk_idx == 0 else _without_comments(kwargs)
                if self._fits(chunk.segments, candidate_images, **eff_kwargs):
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

        def _build(candidate) -> list:
            try:
                return build_messages(candidate, [], **kwargs)
            except TypeError:
                return build_messages(candidate, **kwargs)

        while idx < len(texts):
            # 二分最大可容纳分组（#130 B3）：与 chunk() 同款——fits 单调，线性逐段
            # 试在长输入下每次重建整条消息 O(n²)，二分降到 O(log n) 次构建
            def _fits_group(k) -> bool:
                messages = _build(texts[idx:idx + k])
                return self.estimate(messages) <= self.max_bytes and (
                    not self.max_tokens or self._estimate_tokens(messages) <= self.max_tokens
                )

            if not _fits_group(1):
                raise ValueError("single text block exceeds max_bytes")
            lo, hi = 1, len(texts) - idx
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if _fits_group(mid):
                    lo = mid
                else:
                    hi = mid - 1
            groups.append(texts[idx:idx + lo])
            idx += lo
        return groups
