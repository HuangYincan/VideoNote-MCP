"""get_task_status 轻量化 + get_task_transcript 工具的验证。

背景：`get_task_status` 曾在 SUCCESS 时把 result.json 里的**完整转写**原样返回，
转写含 full_text + 逐段 segments（时间轴），一次工具调用可灌入数万~数十万 token，
直接把 Agent context 撑爆（长视频实测 transcript.json ~280KB/个）。

覆盖点：
1. get_task_status 默认（include_transcript=False）返回**轻量**结果：有
   markdown/note_dir/title，不含 transcript/comments_danmaku；
2. get_task_status(include_transcript=True) 返回全量转写，且剥掉 raw 字段
   （原始 API 响应可能很大）；
3. material 模式同样默认轻量（frames/paths 保留，transcript/comments 需显式取）；
4. 新增 get_task_transcript(task_id)：读 gen/transcript.json，返回完整转写；
5. get_task_transcript(segment_range="0-2")：按段切片 + full_text 拼接 + meta 统计；
6. 越界/未完成任务：ok:false 或钳制到合法区间；
7. wait_for_note 透传 include_transcript。
"""
import json
import shutil
import sys
import threading
import time
import unittest
from pathlib import Path

# 确保能 import videonote_mcp.server（vendored app.* 在其内部 import）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 必须先 import server：其模块顶层 setup_environment() 会把 NOTE_OUTPUT_DIR 等
# 环境变量设为数据目录的绝对路径（pytest 由 conftest.py 隔离到 /tmp/videonote_pytest）
import videonote_mcp.server as server


def _make_success_task(task_id, material=False, with_raw=True, comments=None):
    """在隔离输出目录造一个 SUCCESS 任务：status.json + result.json + gen/transcript.json。"""
    task_dir = server.NOTE_OUTPUT_DIR / task_id
    (task_dir / "gen").mkdir(parents=True, exist_ok=True)
    (task_dir / "status.json").write_text(
        json.dumps({"status": "SUCCESS", "message": "完成"}, ensure_ascii=False),
        encoding="utf-8",
    )
    segments = [
        {"start": 0.0, "end": 1.0, "text": "第一句"},
        {"start": 1.0, "end": 2.0, "text": "第二句"},
        {"start": 2.0, "end": 3.0, "text": "第三句"},
    ]
    transcript = {
        "language": "zh",
        "full_text": "第一句\n第二句\n第三句",
        "segments": segments,
    }
    (task_dir / "gen" / "transcript.json").write_text(
        json.dumps(transcript, ensure_ascii=False), encoding="utf-8"
    )
    payload = {"title": "测试标题", "note_dir": str(task_dir)}
    if material:
        payload.update(
            {
                "kind": "material",
                "frames": ["file:///tmp/frame_1.jpg"],
                "video_path": "/tmp/video.mp4",
                "audio_path": "/tmp/audio.mp3",
                "transcript": dict(transcript, raw={"big": "x" * 1000} if with_raw else None),
                "comments_danmaku": comments or "【弹幕】测试",
            }
        )
    else:
        payload.update(
            {
                "markdown": "# 测试标题\n正文内容",
                "audio_meta": {"title": "测试标题"},
                "transcript": dict(transcript, raw={"big": "x" * 1000} if with_raw else None),
            }
        )
    (task_dir / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return task_dir


class StatusLightTest(unittest.TestCase):
    def setUp(self):
        self.task_id = "abcdef123456"
        self.task_dir = _make_success_task(self.task_id)

    def tearDown(self):
        shutil.rmtree(self.task_dir, ignore_errors=True)

    def test_default_is_light(self):
        resp = json.loads(server.get_task_status(self.task_id))
        self.assertEqual(resp["status"], "SUCCESS")
        result = resp["result"]
        # 轻量必带：markdown / note_dir / title
        self.assertEqual(result["markdown"], "# 测试标题\n正文内容")
        self.assertEqual(result["title"], "测试标题")
        self.assertEqual(result["note_dir"], str(self.task_dir))
        # 关键：默认不含完整转写 / 评论（避免撑爆 context）
        self.assertNotIn("transcript", result)
        self.assertNotIn("comments_danmaku", result)

    def test_include_transcript_returns_full_and_strips_raw(self):
        resp = json.loads(server.get_task_status(self.task_id, include_transcript=True))
        result = resp["result"]
        self.assertIn("transcript", result)
        tr = result["transcript"]
        self.assertEqual(tr["full_text"], "第一句\n第二句\n第三句")
        self.assertEqual(len(tr["segments"]), 3)
        # raw（原始 API 响应）即便要全量也不该返回
        self.assertNotIn("raw", tr)

    def test_material_default_light_but_frames_kept(self):
        mid = "material0001"
        _make_success_task(mid, material=True)
        try:
            resp = json.loads(server.get_task_status(mid))
            result = resp["result"]
            self.assertEqual(result["kind"], "material")
            # 路径类轻量字段保留（Agent 需要它们去 Read 图/找文件）
            self.assertEqual(result["frames"], ["file:///tmp/frame_1.jpg"])
            self.assertEqual(result["video_path"], "/tmp/video.mp4")
            # 文本重载默认剥掉
            self.assertNotIn("transcript", result)
            self.assertNotIn("comments_danmaku", result)
            # 显式要则给
            full = json.loads(server.get_task_status(mid, include_transcript=True))
            self.assertIn("transcript", full["result"])
            self.assertIn("comments_danmaku", full["result"])
        finally:
            shutil.rmtree(server.NOTE_OUTPUT_DIR / mid, ignore_errors=True)


class GetTranscriptTest(unittest.TestCase):
    def setUp(self):
        self.task_id = "transcript0001"
        self.task_dir = _make_success_task(self.task_id)

    def tearDown(self):
        shutil.rmtree(self.task_dir, ignore_errors=True)

    def test_full(self):
        data = json.loads(server.get_task_transcript(self.task_id, "all"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["language"], "zh")
        self.assertEqual(data["full_text"], "第一句\n第二句\n第三句")
        self.assertEqual(len(data["segments"]), 3)
        meta = data["meta"]
        self.assertEqual(meta["total_segments"], 3)
        self.assertEqual(meta["returned_segments"], 3)
        self.assertFalse(meta["truncated"])

    def test_default_caps_at_50(self):
        tid = "longtrans0001"
        tdir = server.NOTE_OUTPUT_DIR / tid
        (tdir / "gen").mkdir(parents=True, exist_ok=True)
        segs = [{"start": float(i), "end": float(i + 1), "text": f"段{i}"} for i in range(80)]
        (tdir / "gen" / "transcript.json").write_text(
            json.dumps({"language": "zh", "full_text": "x", "segments": segs}, ensure_ascii=False),
            encoding="utf-8",
        )
        (tdir / "status.json").write_text(
            json.dumps({"status": "SUCCESS", "message": "完成"}, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            data = json.loads(server.get_task_transcript(tid))
            self.assertTrue(data["ok"])
            self.assertEqual(data["meta"]["total_segments"], 80)
            self.assertEqual(data["meta"]["returned_segments"], 50)
            self.assertTrue(data["meta"]["truncated"])
            full = json.loads(server.get_task_transcript(tid, "all"))
            self.assertEqual(full["meta"]["returned_segments"], 80)
            self.assertFalse(full["meta"]["truncated"])
        finally:
            shutil.rmtree(tdir, ignore_errors=True)

    def test_slice(self):
        data = json.loads(server.get_task_transcript(self.task_id, "0-2"))
        self.assertTrue(data["ok"])
        self.assertEqual([s["text"] for s in data["segments"]], ["第一句", "第二句"])
        self.assertEqual(data["full_text"], "第一句\n第二句")
        self.assertTrue(data["meta"]["truncated"])
        self.assertEqual(data["meta"]["returned_segments"], 2)

    def test_slice_open_end_and_single(self):
        d1 = json.loads(server.get_task_transcript(self.task_id, "1-"))
        self.assertEqual([s["text"] for s in d1["segments"]], ["第二句", "第三句"])
        d2 = json.loads(server.get_task_transcript(self.task_id, "-2"))
        self.assertEqual([s["text"] for s in d2["segments"]], ["第一句", "第二句"])
        d3 = json.loads(server.get_task_transcript(self.task_id, "2"))
        self.assertEqual([s["text"] for s in d3["segments"]], ["第三句"])

    def test_out_of_range_clamped(self):
        data = json.loads(server.get_task_transcript(self.task_id, "50-100"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["segments"], [])
        self.assertEqual(data["full_text"], "")
        self.assertEqual(data["meta"]["returned_segments"], 0)
        self.assertTrue(data["meta"]["truncated"])

    def test_pending_task_ok_false(self):
        pid = "pending000001"
        pdir = server.NOTE_OUTPUT_DIR / pid
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "status.json").write_text(
            json.dumps({"status": "PENDING", "message": "排队中"}, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            data = json.loads(server.get_task_transcript(pid))
            self.assertFalse(data["ok"])
            self.assertEqual(data["status"], "PENDING")
        finally:
            shutil.rmtree(pdir, ignore_errors=True)


class StageElapsedTest(unittest.TestCase):
    def test_stage_label_and_elapsed_from_started_at(self):
        tid = "stage0000001"
        tdir = server.NOTE_OUTPUT_DIR / tid
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "status.json").write_text(
            json.dumps({"status": "TRANSCRIBING", "message": "转写中", "started_at": time.time() - 125}),
            encoding="utf-8",
        )
        try:
            resp = json.loads(server.get_task_status(tid))
            self.assertEqual(resp["status"], "TRANSCRIBING")
            self.assertEqual(resp["stage"], "转写中")
            self.assertGreaterEqual(resp["elapsed_secs"], 124)
        finally:
            shutil.rmtree(tdir, ignore_errors=True)

    def test_missing_started_at_elapsed_none(self):
        tid = "stage0000002"
        tdir = server.NOTE_OUTPUT_DIR / tid
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "status.json").write_text(json.dumps({"status": "PENDING", "message": "排队中"}), encoding="utf-8")
        try:
            resp = json.loads(server.get_task_status(tid))
            self.assertEqual(resp["stage"], "排队中")
            self.assertIsNone(resp["elapsed_secs"])
        finally:
            shutil.rmtree(tdir, ignore_errors=True)


class WaitForNoteTest(unittest.TestCase):
    def setUp(self):
        self.task_id = "waitnote00001"
        self.task_dir = _make_success_task(self.task_id)

    def tearDown(self):
        shutil.rmtree(self.task_dir, ignore_errors=True)

    def test_unknown_task_is_not_found(self):
        resp = json.loads(server.get_task_status("nosuch000001"))
        self.assertEqual(resp["status"], "NOT_FOUND")
        self.assertIsNone(resp["result"])

    def test_wait_unknown_returns_immediately(self):
        resp = json.loads(server.wait_for_note("nosuch000002", timeout=30, poll_interval=5))
        self.assertEqual(resp["status"], "NOT_FOUND")

    def test_wait_forwards_include_transcript(self):
        # SUCCESS 立刻返回；wait_for_note 不再 sleep
        light = json.loads(server.wait_for_note(self.task_id, timeout=1, poll_interval=1))
        self.assertEqual(light["status"], "SUCCESS")
        self.assertNotIn("transcript", light["result"])

        full = json.loads(
            server.wait_for_note(self.task_id, timeout=1, poll_interval=1, include_transcript=True)
        )
        self.assertEqual(full["status"], "SUCCESS")
        self.assertIn("transcript", full["result"])

    def test_wait_pending_does_not_block(self):
        pid = "waitpend00001"
        pdir = server.NOTE_OUTPUT_DIR / pid
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "status.json").write_text(
            json.dumps({"status": "TRANSCRIBING", "message": "转写中"}, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            resp = json.loads(server.wait_for_note(pid, timeout=30, poll_interval=5))
            self.assertEqual(resp["status"], "TRANSCRIBING")
            self.assertTrue(resp.get("deprecated"))
        finally:
            shutil.rmtree(pdir, ignore_errors=True)

    def test_invalid_segment_range_errors(self):
        """非法 segment_range 报 {ok:false}，而不是静默回退全文（#55）。"""
        resp = json.loads(server.get_task_transcript(self.task_id, segment_range="abc"))
        self.assertFalse(resp["ok"])
        self.assertIn("非法", resp["message"])
        resp2 = json.loads(server.get_task_transcript(self.task_id, segment_range="50--60"))
        self.assertFalse(resp2["ok"])

    def test_status_terminal_detection(self):
        """_status_is_terminal：终态 True，进行中/缺失 False（#53）。"""
        pid = "termcheck0001"
        pdir = server.NOTE_OUTPUT_DIR / pid
        pdir.mkdir(parents=True, exist_ok=True)
        try:
            self.assertFalse(server._status_is_terminal(pid))  # 无 status.json
            (pdir / "status.json").write_text(
                json.dumps({"status": "TRANSCRIBING"}, ensure_ascii=False), encoding="utf-8"
            )
            self.assertFalse(server._status_is_terminal(pid))
            (pdir / "status.json").write_text(
                json.dumps({"status": "SUCCESS"}, ensure_ascii=False), encoding="utf-8"
            )
            self.assertTrue(server._status_is_terminal(pid))
        finally:
            shutil.rmtree(pdir, ignore_errors=True)

    def test_cancel_on_terminal_keeps_success(self):
        """终态任务再 cancel：不覆盖 SUCCESS（#53 竞态修复）。"""
        _make_success_task(self.task_id)
        # 模拟竞态窗口：任务已 SUCCESS（终态已写）但 registry 尚未弹出
        from concurrent.futures import Future

        done = Future()
        done.set_result(None)
        with server._tasks_lock:
            server._task_futures[self.task_id] = done
            server._task_events[self.task_id] = threading.Event()
        try:
            resp = json.loads(server.cancel_note(self.task_id))
        finally:
            with server._tasks_lock:
                server._task_futures.pop(self.task_id, None)
                server._task_events.pop(self.task_id, None)
        self.assertEqual(resp["status"], "DONE")
        status = json.loads(
            (server.NOTE_OUTPUT_DIR / self.task_id / "status.json").read_text(encoding="utf-8")
        )
        self.assertEqual(status["status"], "SUCCESS")  # 未被改写成 CANCELLED


if __name__ == "__main__":
    unittest.main()
