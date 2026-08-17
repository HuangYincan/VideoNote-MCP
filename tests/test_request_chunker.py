"""RequestChunker 双约束（字节 + token）与拆分行为（docs/05 #32）。"""
import unittest

from app.gpt.request_chunker import ChunkPayload, RequestChunker


def _messages_size(messages, *_a, **_k):
    import json
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))


class _Seg:
    def __init__(self, text, start=0.0, end=1.0):
        self.text = text
        self.start = start
        self.end = end


def _builder(segments, image_urls, **kwargs):
    return {"segments": [s.text if hasattr(s, "text") else s for s in segments],
            "images": image_urls}


class TestTokenChunking(unittest.TestCase):
    def test_chinese_split_by_token_not_bytes(self):
        # 每段 300 汉字（≈300 token）：max_tokens=700 时应切成多 chunk；
        # 纯字节视角 4 段总共 ~3.6KB 远低于 max_bytes，不会切
        segs = [_Seg("汉" * 300) for _ in range(4)]
        chunker = RequestChunker(
            _builder, max_bytes=1024 * 1024,
            size_estimator=_messages_size,
            max_tokens=700,
        )
        chunks = chunker.chunk(segs, [])
        self.assertGreater(len(chunks), 1, "token 约束下中文长转写应切块")
        # 每块都满足 token 上限
        for c in chunks:
            tokens = chunker._estimate_tokens(_builder(c.segments, []))
            self.assertLessEqual(tokens, 700)

    def test_no_max_tokens_keeps_byte_behavior(self):
        segs = [_Seg("汉" * 300) for _ in range(4)]
        chunker = RequestChunker(_builder, max_bytes=1024 * 1024, size_estimator=_messages_size)
        chunks = chunker.chunk(segs, [])
        self.assertEqual(len(chunks), 1)

    def test_split_segment_fits_both_constraints(self):
        # 单段 2000 汉字，token 上限 500 → 必须拆成多块
        segs = [_Seg("汉" * 2000)]
        chunker = RequestChunker(
            _builder, max_bytes=1024 * 1024,
            size_estimator=_messages_size,
            max_tokens=500,
        )
        chunks = chunker.chunk(segs, [])
        self.assertGreater(len(chunks), 1)
        total = sum(len(c.segments[0].text) for c in chunks)
        self.assertEqual(total, 2000, "拆分不丢内容")


if __name__ == "__main__":
    unittest.main()
