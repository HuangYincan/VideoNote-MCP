"""#125 B5：KuaishouTranscriber / BilibiliDmPatch / screenshot_marker 测试补全。

这 3 个模块此前零用例（生产转写引擎 + 笔记产物正确性）。不碰真实网络，
requests 与 yt_dlp 全 mock。运行：
    cd <repo>
    .venv/bin/python tests/test_b5_coverage.py
"""
import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class KuaishouTranscriberTest(unittest.TestCase):
    """快手转写引擎：成功/空结果/业务错误/网络错误路径（#125 B5）。"""

    def _transcriber(self):
        from app.transcriber.kuaishou import KuaishouTranscriber

        return KuaishouTranscriber()

    def _ok_response(self, texts):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "code": 0,
            "data": {"text": [
                {"text": t, "start_time": str(i * 1000), "end_time": str((i + 1) * 1000)}
                for i, t in enumerate(texts)
            ]},
        }
        return resp

    def test_success_segments_and_join(self):
        tr = self._transcriber()
        with tempfile.NamedTemporaryFile(suffix=".mp3") as f:
            f.write(b"x")
            f.flush()
            with mock.patch("app.transcriber.kuaishou.requests.post", return_value=self._ok_response(["你好", "世界"])):
                result = tr.transcript(f.name)
        self.assertEqual(result.language, "zh")
        self.assertEqual(result.full_text, "你好 世界")
        self.assertEqual([s.text for s in result.segments], ["你好", "世界"])
        self.assertEqual(result.segments[0].start, 0.0)
        self.assertEqual(result.segments[1].end, 2000.0)

    def test_empty_text_result(self):
        tr = self._transcriber()
        with tempfile.NamedTemporaryFile(suffix=".mp3") as f:
            f.write(b"x")
            f.flush()
            with mock.patch("app.transcriber.kuaishou.requests.post", return_value=self._ok_response([])):
                result = tr.transcript(f.name)
        self.assertEqual(result.full_text, "")
        self.assertEqual(result.segments, [])

    def test_business_error_raises_with_message(self):
        tr = self._transcriber()
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"code": 10001, "message": "音频过长"}
        with tempfile.NamedTemporaryFile(suffix=".mp3") as f:
            f.write(b"x")
            f.flush()
            with mock.patch("app.transcriber.kuaishou.requests.post", return_value=resp):
                with self.assertRaises(Exception) as ctx:
                    tr.transcript(f.name)
        self.assertIn("音频过长", str(ctx.exception))

    def test_network_error_keeps_cause(self):
        tr = self._transcriber()
        original = ConnectionError("timeout")
        with tempfile.NamedTemporaryFile(suffix=".mp3") as f:
            f.write(b"x")
            f.flush()
            with mock.patch("app.transcriber.kuaishou.requests.post", side_effect=original):
                with self.assertRaises(ConnectionError) as ctx:
                    tr.transcript(f.name)
        self.assertIs(ctx.exception, original)


class BilibiliDmPatchTest(unittest.TestCase):
    """dm_img 注入补丁：参数形状 / 幂等 / 缺失 yt-dlp 降级（#125 B5）。"""

    def test_build_params_shape(self):
        from app.downloaders.bilibili_dm_patch import build_dm_img_params

        for _ in range(5):
            params = build_dm_img_params()
            self.assertIn("web_location", params)
            self.assertIn("dm_img_list", params)
            self.assertIn("dm_img_str", params)
            self.assertIn("dm_cover_img_str", params)
            self.assertIn("dm_img_inter", params)
            # dm_img_str 是合法 base64（[:-2] 截掉 == 填充）
            base64.b64decode(params["dm_img_str"] + "==")
            self.assertGreater(len(params["dm_img_str"]), 8)

    def test_patch_idempotent_and_injects_query(self):
        # 直接 patch 真实 yt-dlp 模块的类（环境已装 yt_dlp）
        import yt_dlp.extractor.bilibili as bili_mod

        from app.downloaders import bilibili_dm_patch

        fake_ie = mock.Mock()
        orig = mock.Mock()
        orig._bili_dm_patched = False
        fake_ie._download_playinfo = orig

        with mock.patch.object(bili_mod, "BilibiliBaseIE", fake_ie):
            # 第一次应用
            self.assertTrue(bilibili_dm_patch.apply_bilibili_dm_img_patch())
            patched = fake_ie._download_playinfo
            self.assertTrue(patched._bili_dm_patched)
            # 第二次幂等：不再重新包装
            self.assertTrue(bilibili_dm_patch.apply_bilibili_dm_img_patch())
            self.assertIs(fake_ie._download_playinfo, patched)
            # 调用：注入 dm_img 参数 + 透传 query 优先
            patched("fake_self", bvid="bv1", cid="cid1", headers={"h": 1}, query={"qn": 64}, fatal=True)
            call_args = orig.call_args
            # _patched 以位置传 bvid/cid：call('fake_self', 'bv1', 'cid1', ...)
            self.assertEqual(call_args[0][1], "bv1")
            self.assertEqual(call_args[1]["fatal"], True)
            merged = call_args[1]["query"]
            self.assertEqual(merged["qn"], 64)
            self.assertEqual(merged["web_location"], 1550101)

    def test_missing_ytdlp_returns_false(self):
        from app.downloaders.bilibili_dm_patch import apply_bilibili_dm_img_patch

        # sys.modules 值为 None 会让 `from yt_dlp.extractor.bilibili import ...`
        # 抛 ImportError（import 系统语义），apply 捕获后返回 False 不抛
        with mock.patch.dict(sys.modules, {"yt_dlp.extractor.bilibili": None}):
            self.assertFalse(apply_bilibili_dm_img_patch())


class ScreenshotMarkerTest(unittest.TestCase):
    """Screenshot 标记解析：m:ss / [m:ss] / 带星号变体（#125 B5，#122 B5 语义）。"""

    def test_plain_minute_second(self):
        from app.utils.screenshot_marker import extract_screenshot_timestamps

        self.assertEqual(
            extract_screenshot_timestamps("这里 Screenshot-[01:23] 那里"),
            [("Screenshot-[01:23]", 83)],
        )

    def test_no_brackets_variant(self):
        from app.utils.screenshot_marker import extract_screenshot_timestamps

        self.assertEqual(
            extract_screenshot_timestamps("Screenshot-5:07"),
            [("Screenshot-5:07", 307)],
        )

    def test_asterisk_variant_matches_full_marker(self):
        """LLM 输出 *Screenshot-[01:23]*：匹配整个 marker（含星号），
        替换后不残留尾部 *（#122 B5 修复语义）。"""
        from app.utils.screenshot_marker import extract_screenshot_timestamps

        results = extract_screenshot_timestamps("见 *Screenshot-[01:23]* 处")
        self.assertEqual(results, [("*Screenshot-[01:23]*", 83)])

    def test_multiple_markers_and_invalid_skipped(self):
        from app.utils.screenshot_marker import extract_screenshot_timestamps

        md = "A Screenshot-[00:05] B Screenshot-[12:34] C 无标记"
        results = extract_screenshot_timestamps(md)
        self.assertEqual(results, [("Screenshot-[00:05]", 5), ("Screenshot-[12:34]", 754)])

    def test_zero_and_rollover_minutes(self):
        from app.utils.screenshot_marker import extract_screenshot_timestamps

        results = extract_screenshot_timestamps("Screenshot-[00:00] Screenshot-[60:01]")
        self.assertEqual(results, [("Screenshot-[00:00]", 0), ("Screenshot-[60:01]", 3601)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
