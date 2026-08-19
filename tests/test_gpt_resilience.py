"""universal_gpt 韧性测试（#120）：merge 取消、quota 不重试、temperature 降级走 logger。

不碰真实网络 / LLM，全 mock。运行：
    cd <repo>
    .venv/bin/python tests/test_gpt_resilience.py
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.exceptions.task import TaskCancelledError
from app.gpt.universal_gpt import UniversalGPT


def _gpt():
    gpt = UniversalGPT(client=mock.Mock(), model="test-model")
    gpt._max_retry_attempts = 2
    gpt._retry_base_backoff = 0.01
    return gpt


class QuotaErrorTest(unittest.TestCase):
    """配额耗尽是非临时错误：此前命中 429 判定可重试，白等 backoff + 白烧尝试（#120）。"""

    def test_quota_errors_not_retryable(self):
        for msg in (
            "Error code: 429 - insufficient_user_quota",
            "insufficient quota",
            "预扣费额度失败",
            "quota exceeded",
        ):
            self.assertFalse(
                UniversalGPT._is_retryable_error(RuntimeError(msg)), msg
            )

    def test_rate_limit_429_still_retryable(self):
        self.assertTrue(
            UniversalGPT._is_retryable_error(RuntimeError("Error code: 429 - rate limit"))
        )

    def test_quota_failure_single_attempt(self):
        gpt = _gpt()
        with mock.patch.object(
            gpt, "_do_create", side_effect=RuntimeError("insufficient_user_quota")
        ) as m_do_create:
            with self.assertRaises(RuntimeError):
                gpt._chat_completion_create([{"role": "user", "content": "x"}])
            self.assertEqual(m_do_create.call_count, 1)  # 判定不重试，只尝试一次


class MergeCancelTest(unittest.TestCase):
    """cancel 后 merge 组循环仍在烧 LLM 配额——组循环前检查取消（#120）。"""

    def test_merge_aborts_on_cancel_before_calling_llm(self):
        gpt = _gpt()
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(gpt, "_chat_completion_create") as m:
            with self.assertRaises(TaskCancelledError):
                gpt._merge_partials(["partial-1", "partial-2"], None, None, cancel_event=cancel)
        m.assert_not_called()

    def test_merge_proceeds_without_cancel(self):
        gpt = _gpt()
        with mock.patch.object(
            gpt, "_chat_completion_create",
            return_value=mock.Mock(choices=[mock.Mock(
                message=mock.Mock(content="合并结果"), finish_reason="stop")]),
        ) as m:
            result = gpt._merge_partials(["partial-1", "partial-2"], None, None)
        self.assertEqual(result, "合并结果")
        m.assert_called_once()


class TemperatureFallbackTest(unittest.TestCase):
    """temperature 降级提示此前用 print（stdio 模式 stdout 被吞，用户看不到）→ logger（#120）。"""

    def test_fallback_logs_warning_not_print(self):
        gpt = _gpt()
        gpt.client.chat.completions.create.side_effect = [
            RuntimeError("'temperature' does not support 0.7"),
            mock.Mock(choices=[mock.Mock(message=mock.Mock(content="ok"), finish_reason="stop")]),
        ]
        with mock.patch("app.gpt.universal_gpt.logger") as m_logger, \
             mock.patch("builtins.print") as m_print:
            gpt._do_create([{"role": "user", "content": "x"}])
        m_print.assert_not_called()
        self.assertTrue(
            any("不支持自定义 temperature" in str(c) for c in m_logger.warning.call_args_list)
        )


class CheckpointResilienceTest(unittest.TestCase):
    """checkpoint 写失败绝不 raise（#124 B2）：它是恢复辅助，失败只 warning。

    旧实现直接 write_text——磁盘满/权限错误把成功的 LLM 输出变成任务 FAILED；
    在 except 处理器里保存时还会替换掉原始 LLM 异常。
    """

    def _gpt_with_failing_checkpoint(self):
        gpt = _gpt()
        path = mock.Mock()
        path.with_suffix.return_value = path  # tmp_path 同 path
        path.write_text.side_effect = OSError("磁盘已满")
        gpt._checkpoint_path = mock.Mock(return_value=path)
        return gpt

    def test_checkpoint_write_failure_does_not_raise(self):
        gpt = self._gpt_with_failing_checkpoint()
        with mock.patch("app.gpt.universal_gpt.logger") as m_logger:
            gpt._save_checkpoint("k", "sig", ["partial"], "summarize")
        self.assertTrue(
            any("保存 checkpoint 失败" in str(c) for c in m_logger.warning.call_args_list)
        )

    def test_checkpoint_success_still_writes(self):
        gpt = _gpt()
        path = mock.Mock()
        path.with_suffix.return_value = mock.Mock()  # tmp 路径独立
        gpt._checkpoint_path = mock.Mock(return_value=path)
        gpt._save_checkpoint("k", "sig", ["partial"], "merge")
        path.with_suffix.return_value.write_text.assert_called_once()
        path.with_suffix.return_value.replace.assert_called_once()


class EmptyChoicesTest(unittest.TestCase):
    """choices 为空时抛明确错误而非 IndexError（#125 B8）。"""

    def test_empty_choices_raises_clear_error(self):
        gpt = _gpt()
        resp = mock.Mock()
        resp.choices = []
        resp.finish_reason = "stop"
        with self.assertRaises(ValueError) as ctx:
            gpt._first_choice_content(resp)
        self.assertIn("不含任何 choices", str(ctx.exception))

    def test_normal_choice_returns_stripped_content(self):
        gpt = _gpt()
        resp = mock.Mock()
        resp.choices = [mock.Mock()]
        resp.choices[0].message.content = "  内容  "
        self.assertEqual(gpt._first_choice_content(resp), "内容")

    def test_none_choices_also_raises(self):
        gpt = _gpt()
        resp = mock.Mock()
        resp.choices = None
        with self.assertRaises(ValueError):
            gpt._first_choice_content(resp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
