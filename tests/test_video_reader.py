"""effective_frame_interval 封顶逻辑（docs/05 #33）：帧组数超限时自适应拉大间隔。"""
import tempfile
import unittest
from unittest import mock

from app.utils.video_reader import effective_frame_interval


class TestEffectiveFrameInterval(unittest.TestCase):
    def test_short_video_keeps_interval(self):
        # 1 分钟视频、6s 间隔、3×3 网格：600s/6 = 100 帧 ≤ 24 组 × 9 = 216 → 不动
        self.assertEqual(effective_frame_interval(600, 6, 9), 6)

    def test_long_video_stretches_interval(self):
        # 1 小时视频：3600/6 = 600 帧 > 216 → 间隔拉大到 ceil(3600/216) = 17
        self.assertEqual(effective_frame_interval(3600, 6, 9), 17)
        # 组数正好压在封顶线：3600/17 = 211.7 帧 → 23 组 ≤ 24 ✓

    def test_very_long_video(self):
        # 3 小时视频：10800/6 = 1800 帧 → 间隔 ceil(10800/216) = 50
        self.assertEqual(effective_frame_interval(10800, 6, 9), 50)

    def test_bad_inputs_are_safe(self):
        self.assertEqual(effective_frame_interval(0, 6, 9), 6)
        self.assertEqual(effective_frame_interval(-5, 6, 9), 6)
        self.assertEqual(effective_frame_interval(None, 6, 9), 6)
        self.assertEqual(effective_frame_interval(3600, 0, 9), 17)  # 间隔 0 → 按 6 计算后拉伸
        self.assertEqual(effective_frame_interval(3600, 6, 0), 6)


if __name__ == "__main__":
    unittest.main()


class ShortVideoFallbackTest(unittest.TestCase):
    """短视频（帧数不足一组）照常出网格图，不静默零帧（#121 B1）。

    旧逻辑：组不满整批 continue——短视频只有一组残组，产出 0 张网格图，
    上层拿到空 frames 当成功（视频理解空转）。
    """

    def test_partial_group_falls_back_to_grid(self):
        import os
        import shutil
        import tempfile
        from unittest import mock

        from PIL import Image

        from app.utils.video_reader import VideoReader

        frame_dir = tempfile.mkdtemp(prefix="vn_frames_")
        grid_dir = tempfile.mkdtemp(prefix="vn_grid_")
        try:
            # 2 张真实小图（frame_dir 内），远小于 3×3=9 的组容量
            for ts in (0, 6):
                Image.new("RGB", (64, 64), (10, 20, 30)).save(
                    os.path.join(frame_dir, f"frame_{ts:02d}_00.jpg")
                )
            reader = VideoReader("whatever.mp4", grid_size=(3, 3), frame_dir=frame_dir, grid_dir=grid_dir)
            with mock.patch.object(
                VideoReader, "extract_frames",
                return_value=[os.path.join(frame_dir, "frame_00_00.jpg"), os.path.join(frame_dir, "frame_06_00.jpg")],
            ):
                out = reader.run()
            # 不再是 0 张：残组按单组拼接兜底
            self.assertEqual(len(out), 1)
            self.assertTrue(out[0].startswith("data:image/jpeg;base64,"))
        finally:
            shutil.rmtree(frame_dir, ignore_errors=True)
            shutil.rmtree(grid_dir, ignore_errors=True)

    def test_full_groups_then_partial_last_still_skipped(self):
        # 已有完整组时，末尾残组仍跳过（避免稀疏网格图）——行为不变
        import os
        import shutil
        import tempfile
        from unittest import mock

        from PIL import Image

        from app.utils.video_reader import VideoReader

        frame_dir = tempfile.mkdtemp(prefix="vn_frames2_")
        grid_dir = tempfile.mkdtemp(prefix="vn_grid2_")
        try:
            paths = []
            for i in range(10):  # 9 + 1：一组满 + 一组残
                p = os.path.join(frame_dir, f"frame_{i:02d}_00.jpg")
                Image.new("RGB", (64, 64), (10, 20, 30)).save(p)
                paths.append(p)
            reader = VideoReader("whatever.mp4", grid_size=(3, 3), frame_dir=frame_dir, grid_dir=grid_dir)
            with mock.patch.object(VideoReader, "extract_frames", return_value=paths):
                out = reader.run()
            self.assertEqual(len(out), 1)  # 只有满组
        finally:
            shutil.rmtree(frame_dir, ignore_errors=True)
            shutil.rmtree(grid_dir, ignore_errors=True)


class SingleFrameFailureTest(unittest.TestCase):
    """单帧 ffmpeg 失败/超时只跳过该帧，不毁整个抽帧任务（#124 B12）。

    旧实现只捕 CalledProcessError：单帧超时（120s）从 future.result() 冒出，
    外层把整个任务打成「视频处理失败」，已抽出的几百帧全丢。
    """

    def test_calledprocesserror_returns_none(self):
        import subprocess

        from app.utils.video_reader import VideoReader

        with tempfile.TemporaryDirectory() as td:
            reader = VideoReader(video_path="/no/such.mp4", frame_dir=td)
            with mock.patch(
                "app.utils.video_reader.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "ffmpeg"),
            ):
                self.assertIsNone(reader._extract_single_frame(10))

    def test_timeoutexpired_returns_none(self):
        import subprocess

        from app.utils.video_reader import VideoReader

        with tempfile.TemporaryDirectory() as td:
            reader = VideoReader(video_path="/no/such.mp4", frame_dir=td)
            with mock.patch(
                "app.utils.video_reader.subprocess.run",
                side_effect=subprocess.TimeoutExpired("ffmpeg", 120),
            ):
                self.assertIsNone(reader._extract_single_frame(10))


if __name__ == "__main__":
    unittest.main()
