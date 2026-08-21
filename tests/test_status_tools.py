"""task 工具（status/transcript/cancel 三分支，#138 合并自 get_task_status /
get_task_transcript / cancel_note）的验证。

背景：`task(action="status")` 曾在 SUCCESS 时把 result.json 里的**完整转写**原样返回，
转写含 full_text + 逐段 segments（时间轴），一次工具调用可灌入数万~数十万 token，
直接把 Agent context 撑爆（长视频实测 transcript.json ~280KB/个）。

覆盖点：
1. task(task_id) 默认（action="status"）返回**轻量**结果：有
   markdown/note_dir/title，不含 transcript/comments_danmaku；
2. task(task_id, action="transcript", segment_range="all") 返回全量转写，且剥掉 raw 字段
   （原始 API 响应可能很大）；
3. material 模式同样默认轻量（frames/paths 保留，transcript/comments 需显式取）；
4. task(action="transcript")：读 gen/transcript.json，返回完整转写；
5. task(action="transcript", segment_range="0-2")：按段切片 + full_text 拼接 + meta 统计；
6. 越界/未完成任务：ok:false 或钳制到合法区间；
7. task(action="cancel") 的排队/运行中/终态措辞。
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
        resp = json.loads(server.task(self.task_id))
        self.assertEqual(resp["status"], "SUCCESS")
        result = resp["result"]
        # 轻量必带：markdown / note_dir / title
        self.assertEqual(result["markdown"], "# 测试标题\n正文内容")
        self.assertEqual(result["title"], "测试标题")
        self.assertEqual(result["note_dir"], str(self.task_dir))
        # 关键：默认不含完整转写 / 评论（避免撑爆 context）
        self.assertNotIn("transcript", result)
        self.assertNotIn("comments_danmaku", result)

    def test_full_transcript_via_transcript_action(self):
        """include_transcript 参数已移除（#138）：全量转写走 action="transcript" + "all"。"""
        resp = json.loads(server.task(self.task_id, action="transcript", segment_range="all"))
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["full_text"], "第一句\n第二句\n第三句")
        self.assertEqual(len(resp["segments"]), 3)
        # raw（原始 API 响应）即便要全量也不该返回
        self.assertNotIn("raw", resp)

    def test_material_default_light_but_frames_kept(self):
        mid = "material0001"
        _make_success_task(mid, material=True)
        try:
            resp = json.loads(server.task(mid))
            result = resp["result"]
            self.assertEqual(result["kind"], "material")
            # 路径类轻量字段保留（Agent 需要它们去 Read 图/找文件）
            self.assertEqual(result["frames"], ["file:///tmp/frame_1.jpg"])
            self.assertEqual(result["video_path"], "/tmp/video.mp4")
            # 素材包契约（docs 审计 G3）：transcript/comments 是主产物，默认保留；
            # 但 raw（原始 API 响应）恒剥
            self.assertIn("transcript", result)
            self.assertIn("comments_danmaku", result)
            self.assertNotIn("raw", result["transcript"])
            # 显式取全量转写：action="transcript" + "all"（include_transcript 已移除，#138）
            full = json.loads(server.task(mid, action="transcript", segment_range="all"))
            self.assertIn("full_text", full)
            self.assertIn("segments", full)
        finally:
            shutil.rmtree(server.NOTE_OUTPUT_DIR / mid, ignore_errors=True)


class GetTranscriptTest(unittest.TestCase):
    def setUp(self):
        self.task_id = "transcript0001"
        self.task_dir = _make_success_task(self.task_id)

    def tearDown(self):
        shutil.rmtree(self.task_dir, ignore_errors=True)

    def test_full(self):
        data = json.loads(server.task(self.task_id, action="transcript", segment_range="all"))
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
            data = json.loads(server.task(tid, action="transcript"))
            self.assertTrue(data["ok"])
            self.assertEqual(data["meta"]["total_segments"], 80)
            self.assertEqual(data["meta"]["returned_segments"], 50)
            self.assertTrue(data["meta"]["truncated"])
            full = json.loads(server.task(tid, action="transcript", segment_range="all"))
            self.assertEqual(full["meta"]["returned_segments"], 80)
            self.assertFalse(full["meta"]["truncated"])
        finally:
            shutil.rmtree(tdir, ignore_errors=True)

    def test_slice(self):
        data = json.loads(server.task(self.task_id, action="transcript", segment_range="0-2"))
        self.assertTrue(data["ok"])
        self.assertEqual([s["text"] for s in data["segments"]], ["第一句", "第二句"])
        # 切片与全量 full_text（缓存，空格分隔）同一分隔符（#127 A8）
        self.assertEqual(data["full_text"], "第一句 第二句")
        self.assertTrue(data["meta"]["truncated"])
        self.assertEqual(data["meta"]["returned_segments"], 2)

    def test_slice_open_end_and_single(self):
        d1 = json.loads(server.task(self.task_id, action="transcript", segment_range="1-"))
        self.assertEqual([s["text"] for s in d1["segments"]], ["第二句", "第三句"])
        d2 = json.loads(server.task(self.task_id, action="transcript", segment_range="-2"))
        self.assertEqual([s["text"] for s in d2["segments"]], ["第一句", "第二句"])
        d3 = json.loads(server.task(self.task_id, action="transcript", segment_range="2"))
        self.assertEqual([s["text"] for s in d3["segments"]], ["第三句"])

    def test_out_of_range_clamped(self):
        data = json.loads(server.task(self.task_id, action="transcript", segment_range="50-100"))
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
            data = json.loads(server.task(pid, action="transcript"))
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
            resp = json.loads(server.task(tid))
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
            resp = json.loads(server.task(tid))
            self.assertEqual(resp["stage"], "排队中")
            self.assertIsNone(resp["elapsed_secs"])
        finally:
            shutil.rmtree(tdir, ignore_errors=True)


class CancelRaceTest(unittest.TestCase):
    """cancel_note 两方向（#121 C5）：排队→CANCELLED+弹 registry；运行中→只发信号。

    WaitForNoteTest 的 setUp 会造 SUCCESS 终态任务（取消走 DONE 分支），
    这里独立成类保证起点没有 status.json。
    """

    def setUp(self):
        self.task_id = "cancelrace0001"
        self.task_dir = server.NOTE_OUTPUT_DIR / self.task_id
        self.task_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.task_dir, ignore_errors=True)
        with server._tasks_lock:
            server._task_futures.pop(self.task_id, None)
            server._task_events.pop(self.task_id, None)
            server._status_memory.pop(self.task_id, None)

    def test_cancel_queued_task_writes_cancelled_and_pops(self):
        """排队中（future.cancel() 成功）→ 写 CANCELLED + 移出 registry（#121 C5）。"""
        from concurrent.futures import Future

        queued = Future()  # 未启动：not done → cancel() 返回 True
        ev = threading.Event()
        with server._tasks_lock:
            server._task_futures[self.task_id] = queued
            server._task_events[self.task_id] = ev
        try:
            resp = json.loads(server.task(self.task_id, action="cancel"))
        finally:
            with server._tasks_lock:
                server._task_futures.pop(self.task_id, None)
                server._task_events.pop(self.task_id, None)
        self.assertEqual(resp["status"], "CANCELLED")
        status = json.loads(
            (server.NOTE_OUTPUT_DIR / self.task_id / "status.json").read_text(encoding="utf-8")
        )
        self.assertEqual(status["status"], "CANCELLED")
        # registry 已弹：worker 不会执行，不会出现二次写
        self.assertNotIn(self.task_id, server._task_futures)

    def test_cancel_running_task_returns_cancelling_signal(self):
        """运行中（future.cancel() 失败）→ 只发协作信号，终态由 worker 写（#121 C5）。

        此前 cancel 直接写盘 CANCELLED：检查与写入之间 worker 完成时把刚写的
        SUCCESS 覆盖成「已取消」，而 result.json 已有完整笔记。
        """
        from concurrent.futures import Future

        running = Future()
        running.set_running_or_notify_cancel()  # running → cancel() 返回 False
        ev = threading.Event()
        with server._tasks_lock:
            server._task_futures[self.task_id] = running
            server._task_events[self.task_id] = ev
        try:
            resp = json.loads(server.task(self.task_id, action="cancel"))
        finally:
            with server._tasks_lock:
                server._task_futures.pop(self.task_id, None)
                server._task_events.pop(self.task_id, None)
        self.assertEqual(resp["status"], "CANCELLING")
        self.assertIn("下一阶段边界", resp["message"])
        self.assertTrue(ev.is_set())  # 信号已发
        # 不写盘：终态留给 worker（避免与 worker 的 SUCCESS/FAILED 写竞争）
        self.assertFalse((server.NOTE_OUTPUT_DIR / self.task_id / "status.json").exists())


class CancelTerminalWordingTest(unittest.TestCase):
    """cancel_note 终态措辞区分（#122 A8）。

    任务恰好在取消时到达终态：此前一律「任务已完成」——FAILED 任务被 Agent
    误读成「成功产出笔记」。按终态区分措辞。
    """

    def setUp(self):
        self.task_id = "termword0001"
        self.task_dir = server.NOTE_OUTPUT_DIR / self.task_id
        self.task_dir.mkdir(parents=True, exist_ok=True)
        from concurrent.futures import Future

        self._done = Future()
        self._done.set_result(None)
        self._ev = threading.Event()
        with server._tasks_lock:
            server._task_futures[self.task_id] = self._done
            server._task_events[self.task_id] = self._ev

    def tearDown(self):
        shutil.rmtree(self.task_dir, ignore_errors=True)
        with server._tasks_lock:
            server._task_futures.pop(self.task_id, None)
            server._task_events.pop(self.task_id, None)

    def _set_status(self, status):
        (self.task_dir / "status.json").write_text(
            json.dumps({"status": status}, ensure_ascii=False), encoding="utf-8"
        )

    def test_failed_terminal_message(self):
        self._set_status("FAILED")
        resp = json.loads(server.task(self.task_id, action="cancel"))
        self.assertEqual(resp["status"], "DONE")
        self.assertIn("失败", resp["message"])
        self.assertNotIn("完成", resp["message"])

    def test_cancelled_terminal_message(self):
        self._set_status("CANCELLED")
        resp = json.loads(server.task(self.task_id, action="cancel"))
        self.assertEqual(resp["status"], "DONE")
        self.assertIn("已取消", resp["message"])

    def test_success_terminal_message(self):
        self._set_status("SUCCESS")
        resp = json.loads(server.task(self.task_id, action="cancel"))
        self.assertEqual(resp["status"], "DONE")
        self.assertIn("已完成", resp["message"])


