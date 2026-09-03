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


class DouyinShortUrlTest(unittest.TestCase):
    """v.douyin.com 短链先解真实链接再提 id（#125 B1）。

    旧实现只匹配 /video/(\\d+)——App 分享默认短链解析不出 → 缓存身份 douyin:None
    永不命中，同一视频每次都重下重转写。
    """

    def test_short_url_resolved_before_extract(self):
        from unittest import mock

        from app.utils.url_parser import extract_video_id

        with mock.patch("app.utils.url_parser.resolve_douyin_short_url") as m_resolve:
            m_resolve.return_value = "https://www.douyin.com/video/7234567890123456789"
            vid = extract_video_id("https://v.douyin.com/abc123/", "douyin")
        m_resolve.assert_called_once_with("https://v.douyin.com/abc123/")
        self.assertEqual(vid, "7234567890123456789")

    def test_resolve_failure_falls_back_to_none(self):
        from app.utils.url_parser import extract_video_id

        self.assertIsNone(extract_video_id("https://v.douyin.com/abc123/", "douyin"))

    def test_full_url_unresolved_keeps_working(self):
        from app.utils.url_parser import extract_video_id

        self.assertEqual(
            extract_video_id("https://www.douyin.com/video/7234567890123456789", "douyin"),
            "7234567890123456789",
        )

    def test_share_url_matches(self):
        from app.utils.url_parser import extract_video_id

        self.assertEqual(
            extract_video_id("https://www.iesdouyin.com/share/video/7234567890123456789", "douyin"),
            "7234567890123456789",
        )


class XiaoyuzhouEpisodeIdTest(unittest.TestCase):
    def test_episode_path(self):
        self.assertEqual(
            extract_video_id(
                "https://www.xiaoyuzhoufm.com/episode/69b3b675772ac2295bfc01d0",
                "xiaoyuzhou",
            ),
            "69b3b675772ac2295bfc01d0",
        )

    def test_podcast_path_not_episode(self):
        self.assertIsNone(
            extract_video_id(
                "https://www.xiaoyuzhoufm.com/podcast/6013f9f58e2f7ee375cf4216",
                "xiaoyuzhou",
            )
        )

    def test_xiaohongshu_explore(self):
        self.assertEqual(
            extract_video_id(
                "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9",
                "xiaohongshu",
            ),
            "6411cf99000000001300b6d9",
        )


if __name__ == "__main__":
    unittest.main()
