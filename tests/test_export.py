"""多格式导出 + 平台接手 handoff 的单元测试。

覆盖：
1. SRT / VTT / JSON 渲染：时间格式、`-->` 转义、多段、空 segments、毫秒进位；
2. exporter 落盘：临时目录、返回 file:// 路径、manifest 记录、未知格式忽略；
3. detect_platform / handoff_result：合法平台不变、未知 URL 返回 unsupported + handoff。

不碰真实网络 / 转写引擎 / LLM / DB。

运行：
    cd <repo>
    .venv/bin/python tests/test_export.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.transcriber_model import TranscriptSegment
from app.services import pipeline
from videonote_mcp.export import export_transcript
from videonote_mcp.export.json import to_json
from videonote_mcp.export.srt import to_srt
from videonote_mcp.export.vtt import to_vtt


def _segs():
    return [
        TranscriptSegment(start=0.0, end=3.5, text="你好，世界"),
        TranscriptSegment(start=3.5, end=6.75, text="Second line --> arrow"),
        TranscriptSegment(start=59.9999, end=61.0, text="进位测试"),
    ]


class SrtTest(unittest.TestCase):
    def test_basic_timestamps(self):
        out = to_srt([TranscriptSegment(0, 3.5, "hi")])
        self.assertIn("00:00:00,000 --> 00:00:03,500", out)
        self.assertIn("hi", out)

    def test_arrow_escaped(self):
        out = to_srt([TranscriptSegment(0, 1, "a --> b")])
        self.assertIn("a → b", out)
        self.assertNotIn("a --> b", out)

    def test_millisecond_carry(self):
        # 59.9999s 的毫秒进位 → 00:01:00,000
        out = to_srt(_segs())
        self.assertIn("00:01:00,000 --> 00:01:01,000", out)

    def test_empty_segments(self):
        self.assertEqual(to_srt([]), "")
        self.assertEqual(to_srt(None), "")

    def test_negative_seconds_clamped(self):
        out = to_srt([TranscriptSegment(-1.0, 0.5, "x")])
        self.assertIn("00:00:00,000", out)


class VttTest(unittest.TestCase):
    def test_header(self):
        self.assertTrue(to_vtt([]).startswith("WEBVTT\n\n"))

    def test_dot_timestamps(self):
        out = to_vtt([TranscriptSegment(0, 3.5, "hi")])
        self.assertIn("00:00:00.000 --> 00:00:03.500", out)

    def test_arrow_escaped(self):
        out = to_vtt([TranscriptSegment(0, 1, "a --> b")])
        self.assertNotIn("a --> b", out)


class JsonTest(unittest.TestCase):
    def test_structure(self):
        data = json.loads(to_json({"language": "zh", "full_text": "hello", "segments": _segs()}))
        self.assertEqual(data["language"], "zh")
        self.assertEqual(len(data["segments"]), 3)
        self.assertEqual(data["segments"][0]["start"], 0.0)
        self.assertEqual(data["segments"][0]["text"], "你好，世界")

    def test_none_source(self):
        data = json.loads(to_json(None))
        self.assertEqual(data["segments"], [])
        self.assertIsNone(data["language"])

    def test_object_source(self):
        from app.models.transcriber_model import TranscriptResult

        tr = TranscriptResult(language="en", full_text="abc", segments=[TranscriptSegment(1, 2, "b")])
        data = json.loads(to_json(tr))
        self.assertEqual(data["language"], "en")
        self.assertEqual(data["segments"][0]["end"], 2.0)

    def test_preserves_speaker_on_dict_segment(self):
        """#122 A2：说话人分离结果导出 JSON 时保留 speaker 字段。"""
        data = json.loads(
            to_json(
                {
                    "language": "zh",
                    "full_text": "x",
                    "segments": [
                        {"start": 0.0, "end": 2.0, "text": "甲", "speaker": "SPEAKER_00"},
                        {"start": 2.0, "end": 4.0, "text": "乙", "speaker": "SPEAKER_01"},
                    ],
                }
            )
        )
        self.assertEqual(data["segments"][0]["speaker"], "SPEAKER_00")
        self.assertEqual(data["segments"][1]["speaker"], "SPEAKER_01")

    def test_preserves_speaker_on_object_segment(self):
        """#122 A2：TranscriptResult 对象的 speaker 同样保留。"""
        from app.models.transcriber_model import TranscriptResult

        tr = TranscriptResult(
            language="zh",
            full_text="x",
            segments=[
                TranscriptSegment(0, 2, "甲", speaker="SPEAKER_00"),
                TranscriptSegment(2, 4, "乙"),
            ],
        )
        data = json.loads(to_json(tr))
        self.assertEqual(data["segments"][0]["speaker"], "SPEAKER_00")
        self.assertNotIn("speaker", data["segments"][1])  # 无 speaker 不产出空字段


class ExporterTest(unittest.TestCase):
    def test_writes_all_formats_to_file_uris(self):
        with tempfile.TemporaryDirectory() as d:
            result = export_transcript(
                {"language": "zh", "full_text": "x", "segments": _segs()},
                formats=["srt", "vtt", "json"],
                out_dir=d,
                task_id="t1",
            )
            self.assertEqual(sorted(result.keys()), ["json", "srt", "vtt"])
            for fmt, uri in result.items():
                self.assertTrue(uri.startswith("file://"))
                p = Path(uri.replace("file://", ""))
                self.assertTrue(p.exists())

    def test_unknown_format_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            result = export_transcript(
                {"segments": _segs()}, formats=["srt", "bogus"], out_dir=d, task_id="t2"
            )
            self.assertEqual(sorted(result.keys()), ["srt"])

    def test_empty_formats_list_exports_nothing(self):
        """#122 A4：显式 formats=[] 必须零导出（旧实现被 `or ["srt"]` 重解释成默认 srt）。"""
        with tempfile.TemporaryDirectory() as d:
            result = export_transcript(
                {"segments": _segs()}, formats=[], out_dir=d, task_id="t4"
            )
            self.assertEqual(result, {})
            self.assertFalse(list(Path(d).glob("transcript.*")))

    def test_manifest_recorded(self):
        with tempfile.TemporaryDirectory() as d, mock.patch(
            "videonote_mcp.export.exporter.record_task_paths"
        ) as rec:
            export_transcript({"segments": _segs()}, formats=["srt"], out_dir=d, task_id="t3")
            rec.assert_called_once()
            # 记录的是 srt 文件的路径
            self.assertTrue(str(rec.call_args[0][1][0]).endswith("transcript.srt"))

    def test_json_export_does_not_overwrite_transcript_cache(self):
        """#122 A2：json 导出写 transcript.export.json，绝不覆盖转写缓存 gen/transcript.json。

        note.py 的转写缓存规范来源就是 gen/transcript.json（server 的读取/fallback 都读它）；
        导出 json 若同名覆盖会把完整缓存（含 raw）替换成轻量导出 JSON。
        """
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "transcript.json"
            cache.write_text('{"cache": "keep-me"}', encoding="utf-8")
            result = export_transcript(
                {"language": "zh", "full_text": "x", "segments": _segs()},
                formats=["json"],
                out_dir=d,
                task_id="t6",
            )
            self.assertIn("json", result)
            self.assertEqual(cache.read_text(encoding="utf-8"), '{"cache": "keep-me"}')
            self.assertTrue((Path(d) / "transcript.export.json").is_file())

    def test_write_failure_reports_ok_false(self):
        """#122 A1：任一格式落盘失败 → 工具层 ok:False + errors（此前全部失败仍 ok:True）。"""
        import videonote_mcp.server as srv

        tid = "exportfail01"
        task_dir = srv.NOTE_OUTPUT_DIR / tid
        (task_dir / "gen").mkdir(parents=True, exist_ok=True)
        (task_dir / "gen" / "transcript.json").write_text(
            json.dumps(
                {
                    "language": "zh",
                    "full_text": "x",
                    "segments": [{"start": 0.0, "end": 1.0, "text": "a"}],
                }
            ),
            encoding="utf-8",
        )
        # C1 门禁（#126）：非 SUCCESS 任务拒绝导出——测试任务补 SUCCESS 状态
        (task_dir / "status.json").write_text(
            json.dumps({"status": "SUCCESS"}, ensure_ascii=False), encoding="utf-8"
        )
        try:
            # B10（docs/05 第 16 轮）：导出走 write_text_atomic（原子写），mock 其底层写
            with mock.patch(
                "videonote_mcp.export.exporter.write_text_atomic",
                side_effect=OSError("disk full"),
            ):
                data = json.loads(srv.process_media(action="export", task_id=tid, formats=["srt", "json"]))
        finally:
            import shutil as _sh

            _sh.rmtree(task_dir, ignore_errors=True)
        self.assertEqual(data["ok"], False)
        self.assertEqual(data["formats"], {})
        self.assertIn("srt", data["errors"])
        self.assertIn("json", data["errors"])

    def test_write_success_reports_ok_true(self):
        """#122 A1：全部落盘成功 → ok:True + errors:{}（成功路径形状稳定）。"""
        import videonote_mcp.server as srv

        tid = "exportok0001"
        task_dir = srv.NOTE_OUTPUT_DIR / tid
        (task_dir / "gen").mkdir(parents=True, exist_ok=True)
        (task_dir / "gen" / "transcript.json").write_text(
            json.dumps(
                {
                    "language": "zh",
                    "full_text": "x",
                    "segments": [{"start": 0.0, "end": 1.0, "text": "a"}],
                }
            ),
            encoding="utf-8",
        )
        # C1 门禁（#126）：非 SUCCESS 任务拒绝导出——测试任务补 SUCCESS 状态
        (task_dir / "status.json").write_text(
            json.dumps({"status": "SUCCESS"}, ensure_ascii=False), encoding="utf-8"
        )
        try:
            data = json.loads(srv.process_media(action="export", task_id=tid, formats=["srt"]))
        finally:
            import shutil as _sh

            _sh.rmtree(task_dir, ignore_errors=True)
        self.assertEqual(data["ok"], True)
        self.assertEqual(data["errors"], {})
        self.assertIn("srt", data["formats"])


class PlatformHandoffTest(unittest.TestCase):
    def test_supported_platforms(self):
        self.assertEqual(pipeline.detect_platform("https://www.bilibili.com/video/BV1xx"), "bilibili")
        self.assertEqual(pipeline.detect_platform("https://www.youtube.com/watch?v=abc"), "youtube")
        self.assertEqual(pipeline.detect_platform("https://v.douyin.com/x"), "douyin")
        self.assertEqual(
            pipeline.detect_platform("https://www.xiaoyuzhoufm.com/episode/69b3b675772ac2295bfc01d0"),
            "xiaoyuzhou",
        )
        self.assertEqual(pipeline.detect_platform("/Users/x/video.mp4"), "local")

    def test_unknown_returns_generic(self):
        # 未知 URL → generic（yt-dlp 通用提取），不再返回 unsupported
        self.assertEqual(pipeline.detect_platform("https://unsupported.example.com/v"), "generic")
        self.assertEqual(pipeline.detect_platform("https://www.example.org/watch/123"), "generic")

    def test_empty_url_raises(self):
        with self.assertRaises(ValueError):
            pipeline.detect_platform("")

    def test_handoff_result_structure(self):
        r = pipeline.handoff_result("https://unsupported.example.com/v")
        self.assertTrue(r["handoff"])
        self.assertEqual(r["platform"], "unsupported")
        self.assertEqual(r["ok"], False)
        self.assertIn("hint", r)
        self.assertIn("url", r)


if __name__ == "__main__":
    unittest.main()
