"""effective_frame_interval 封顶逻辑（docs/05 #33）：帧组数超限时自适应拉大间隔。"""
import unittest

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
