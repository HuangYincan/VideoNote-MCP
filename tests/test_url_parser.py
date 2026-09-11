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


class DouyinAwemeIdTest(unittest.TestCase):
    """同一条抖音视频的多种 URL 归一为 aweme_id。

    旧实现只匹配 /video/(\\d+)——精选页 ``?modal_id=`` 与短链跳到精选页时
    解析不出 id，缓存身份 douyin:None，同一视频每次重下重转写。
    """

    VIDEO_ID = "7664861474845658402"

    def test_short_url_resolved_before_extract(self):
        from unittest import mock

        with mock.patch("app.utils.url_parser.resolve_douyin_short_url") as m_resolve:
            m_resolve.return_value = "https://www.douyin.com/video/7234567890123456789"
            vid = extract_video_id("https://v.douyin.com/abc123/", "douyin")
        m_resolve.assert_called_once_with("https://v.douyin.com/abc123/")
        self.assertEqual(vid, "7234567890123456789")

    def test_short_url_resolved_to_jingxuan_modal_id(self):
        from unittest import mock

        with mock.patch("app.utils.url_parser.resolve_douyin_short_url") as m_resolve:
            m_resolve.return_value = (
                f"https://www.douyin.com/jingxuan?modal_id={self.VIDEO_ID}"
            )
            vid = extract_video_id("https://v.douyin.com/_pnGDe3Hk3E/", "douyin")
        m_resolve.assert_called_once_with("https://v.douyin.com/_pnGDe3Hk3E/")
        self.assertEqual(vid, self.VIDEO_ID)

    def test_resolve_failure_falls_back_to_none(self):
        from unittest import mock

        with mock.patch(
            "app.utils.url_parser.resolve_douyin_short_url", return_value=None
        ):
            self.assertIsNone(extract_video_id("https://v.douyin.com/abc123/", "douyin"))

    def test_full_url_unresolved_keeps_working(self):
        self.assertEqual(
            extract_video_id("https://www.douyin.com/video/7234567890123456789", "douyin"),
            "7234567890123456789",
        )

    def test_share_url_matches(self):
        self.assertEqual(
            extract_video_id("https://www.iesdouyin.com/share/video/7234567890123456789", "douyin"),
            "7234567890123456789",
        )

    def test_jingxuan_modal_id(self):
        from unittest import mock

        with mock.patch("app.utils.url_parser.resolve_douyin_short_url") as m_resolve:
            self.assertEqual(
                extract_video_id(
                    f"https://www.douyin.com/jingxuan?modal_id={self.VIDEO_ID}",
                    "douyin",
                ),
                self.VIDEO_ID,
            )
        m_resolve.assert_not_called()

    def test_user_and_discover_modal_id(self):
        self.assertEqual(
            extract_video_id(
                f"https://www.douyin.com/user/MS4wLjABAAAA?modal_id={self.VIDEO_ID}",
                "douyin",
            ),
            self.VIDEO_ID,
        )
        self.assertEqual(
            extract_video_id(
                f"https://www.douyin.com/discover?modal_id={self.VIDEO_ID}&from=web",
                "douyin",
            ),
            self.VIDEO_ID,
        )

    def test_equivalent_forms_share_same_id(self):
        from unittest import mock

        video = f"https://www.douyin.com/video/{self.VIDEO_ID}"
        jingxuan = f"https://www.douyin.com/jingxuan?modal_id={self.VIDEO_ID}"
        self.assertEqual(extract_video_id(video, "douyin"), self.VIDEO_ID)
        self.assertEqual(extract_video_id(jingxuan, "douyin"), self.VIDEO_ID)
        with mock.patch("app.utils.url_parser.resolve_douyin_short_url") as m_resolve:
            m_resolve.return_value = jingxuan
            self.assertEqual(
                extract_video_id("https://v.douyin.com/_pnGDe3Hk3E/", "douyin"),
                self.VIDEO_ID,
            )

    def test_modal_id_preferred_over_path(self):
        self.assertEqual(
            extract_video_id(
                f"https://www.douyin.com/video/1111111111111111111?modal_id={self.VIDEO_ID}",
                "douyin",
            ),
            self.VIDEO_ID,
        )

    def test_share_text_extracts_embedded_short_url(self):
        from unittest import mock

        share = (
            "7.43 11/16 gba:/ j@P.xS 标题 https://v.douyin.com/_pnGDe3Hk3E/ "
            "复制此链接，打开Dou音搜索，直接观看视频！"
        )
        with mock.patch("app.utils.url_parser.resolve_douyin_short_url") as m_resolve:
            m_resolve.return_value = (
                f"https://www.douyin.com/video/{self.VIDEO_ID}"
            )
            vid = extract_video_id(share, "douyin")
        m_resolve.assert_called_once_with("https://v.douyin.com/_pnGDe3Hk3E/")
        self.assertEqual(vid, self.VIDEO_ID)

    def test_feed_without_modal_id_is_not_a_video(self):
        from unittest import mock

        with mock.patch("app.utils.url_parser.resolve_douyin_short_url") as m_resolve:
            self.assertIsNone(
                extract_video_id("https://www.douyin.com/jingxuan", "douyin")
            )
        m_resolve.assert_not_called()


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
