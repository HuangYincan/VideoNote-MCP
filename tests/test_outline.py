"""大纲提取 / 注入（docs/05 #39 标题漂移）。

不碰真实网络 / LLM，mock _chat_completion_create 验证:
- extract_outline 提取/清理/去重/截断
- generate_base_prompt 与 create_messages 的 outline 透传
- 多 chunk 时第二个请求的 prompt 含第一个 partial 的标题；单 chunk 不注入
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app.gpt.prompt_builder import generate_base_prompt
from app.gpt.universal_gpt import UniversalGPT, extract_outline
from app.models.gpt_model import GPTSource
from app.models.transcriber_model import TranscriptSegment


def _segments(texts):
    start = 0.0
    out = []
    for t in texts:
        out.append(TranscriptSegment(start=start, end=start + 5.0, text=t))
        start += 5.0
    return out


def _gpt(segments, max_tokens=1000, checkpoint_dir=None):
    # 模板+1 段 ≈830 token、2 段 ≈1140：max_tokens=1000 时每 chunk 恰好 1 段，
    # 强制切成多 chunk（边界宽 ~170 token，不随模板微调漂移）。
    gpt = UniversalGPT(client=mock.Mock(), model="test-model")
    gpt.max_tokens_per_chunk = max_tokens
    if checkpoint_dir:
        gpt.checkpoint_dir = checkpoint_dir
    return gpt


def _long_segments(n):
    return _segments(["这是第%d段的内容。" % i + "内容" * 150 for i in range(n)])


def _captured_prompts(gpt, segments, title="测试视频"):
    """跑 summarize 并捕获每次 LLM 调用的 prompt 文本（含 merge 调用）。"""
    source = GPTSource(title=title, segment=segments, tags="", checkpoint_key=None)
    captured = []

    def capture(messages):
        captured.append(messages[0]["content"])
        return mock.Mock(choices=[mock.Mock(
            message=mock.Mock(content="## 结果"), finish_reason="stop")])

    with mock.patch.object(gpt, "_chat_completion_create", side_effect=capture):
        gpt.summarize(source)
    return captured


class TestExtractOutline:
    def test_extracts_headings_and_cleans_markdown(self):
        partial = (
            "## AI 的发展史 *Content-[01:23]\n"
            "正文\n"
            "### 深度学习的**突破**\n"
            "正文\n"
            "# 总结\n"
            "## 应用场景\n"
        )
        lines = extract_outline([partial]).splitlines()
        assert lines == ["- AI 的发展史", "- 深度学习的突破", "- 总结", "- 应用场景"]

    def test_dedup_and_limit(self):
        partials = ["## 第一节\n内容", "## 第一节\n内容2", "## 第二节\n内容"]
        assert extract_outline(partials, limit=2) == "- 第一节\n- 第二节"
        assert extract_outline(partials) == "- 第一节\n- 第二节"

    def test_truncates_long_titles(self):
        outline = extract_outline(["## " + "长" * 60 + "\n"], max_title_len=20)
        assert outline == "- " + "长" * 20

    def test_empty_input(self):
        assert extract_outline([]) == ""
        assert extract_outline(None) == ""
        assert extract_outline(["无标题内容\n普通段落"]) == ""

    def test_ignores_lower_than_h4(self):
        assert extract_outline(["## h2\n### h3\n#### h4\n##### h5\n- 列表"]) \
            == "- h2\n- h3\n- h4"


class TestOutlineInjection:
    def test_generate_base_prompt_injects_block(self):
        prompt = generate_base_prompt(
            title="t", segment_text="s", tags="", outline="- 第一节\n- 第二节")
        assert "已生成的章节大纲" in prompt
        assert "- 第一节\n- 第二节" in prompt
        assert "不要重复创建相同或同义的章节标题" in prompt

    def test_generate_base_prompt_without_outline(self):
        prompt = generate_base_prompt(title="t", segment_text="s", tags="")
        assert "已生成的章节大纲" not in prompt

    def test_create_messages_passes_outline(self):
        gpt = _gpt(_segments(["hello"]))
        messages = gpt.create_messages(_segments(["hello"]), outline="- 已有章节")
        assert "已生成的章节大纲" in messages[0]["content"]

    def test_single_chunk_no_outline(self):
        gpt = _gpt(_long_segments(1))
        prompts = _captured_prompts(gpt, _long_segments(1))
        assert len(prompts) == 1
        assert "已生成的章节大纲" not in prompts[0]

    def test_second_chunk_carries_first_partial_outline(self):
        gpt = _gpt(_long_segments(3))
        prompts = _captured_prompts(gpt, _long_segments(3))
        assert len(prompts) == 4  # chunk1 + chunk2 + chunk3 + merge
        # 第一个 chunk 无大纲；第二个 chunk 必须携带第一个 partial 的标题
        assert "已生成的章节大纲" not in prompts[0]
        assert "已生成的章节大纲" in prompts[1]
        assert "结果" in prompts[1]

    def test_checkpoint_recovery_reinjects_outline(self):
        with TemporaryDirectory() as d:
            gpt = _gpt(_long_segments(4), checkpoint_dir=Path(d))
            source = GPTSource(
                title="t", segment=_long_segments(4), tags="", checkpoint_key="ck")
            captured = []

            def capture(messages):
                captured.append(messages[0]["content"])
                return mock.Mock(choices=[mock.Mock(
                    message=mock.Mock(content="## 第二章\n内容"), finish_reason="stop")])

            with mock.patch.object(gpt, "_load_checkpoint",
                                   return_value={"partials": ["## 第一章\n内容"], "phase": "summarize"}), \
                 mock.patch.object(gpt, "_chat_completion_create", side_effect=capture):
                result = gpt.summarize(source)

            assert "第二章" in result  # merge 产物（capture 固定返回该值）
            # 恢复的 partial 不算新 chunk：LLM 只被调用于剩余 chunk + merge
            assert len(captured) >= 1
            # 剩余 chunk 的 prompt 注入已恢复 partial 的标题
            assert "已生成的章节大纲" in captured[0]
            assert "第一章" in captured[0]
