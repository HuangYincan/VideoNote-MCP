"""extract_video_id 的 URL 形态覆盖（#121 B9）：补 youtube.com/embed/{id}。

其余形态（watch?v= / youtu.be/ / shorts/）回归保护。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.utils.url_parser import extract_video_id


class YoutubeEmbedTest(unittest.TestCase):
    def test_embed_path(self):
        # embed 形态此前漏匹配（正则只有 v=/youtu.be//shorts/）→ 返回 None
        self.assertEqual(
            extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ", "youtube"),
            "dQw4w9WgXcQ",
        )

    def test_embed_with_query(self):
        self.assertEqual(
            extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ?si=abc123", "youtube"),
            "dQw4w9WgXcQ",
        )

    def test_embed_with_timestamp(self):
        self.assertEqual(
            extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ?start=15", "youtube"),
            "dQw4w9WgXcQ",
        )

    def test_watch_shorts_youtu_be_unchanged(self):
        self.assertEqual(
            extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
            "dQw4w9WgXcQ",
        )
        self.assertEqual(
            extract_video_id("https://youtu.be/dQw4w9WgXcQ", "youtube"), "dQw4w9WgXcQ"
        )
        self.assertEqual(
            extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ", "youtube"),
            "dQw4w9WgXcQ",
        )

    def test_non_youtube_platform_untouched(self):
        self.assertIsNone(extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ", "douyin"))


if __name__ == "__main__":
    unittest.main()
