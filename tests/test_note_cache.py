"""跨任务转写缓存（note_cache）：身份解析 / 分键 / 命中 / promote / generate 集成。

背景：同一视频再次 `generate_note` = 完整重下载 + 重转写（转写是流水线最贵环节）。
note_cache 按 `platform:video_id` 缓存上次转写，命中时把缓存 transcript 拷进
`{task_id}/gen/transcript.json`——下游 has_transcript → skip_download 即跳过下载与转写。

覆盖点：
1. 身份键：B 站 BV+p / YouTube v= / 抖音 / TikTok / 本地 sha256 / 快手等解析不出；
2. engine_key：本地引擎拼 model_size，云端引擎不拼；切换引擎/尺寸不误用旧结果；
3. 命中 → 拷贝到任务 gen/、miss 时返回 None；
4. promote → 再 lookup 命中；bilibili `_pN` 后缀归一化；
5. generate() 集成：预置缓存后跑 material_only，断言只做元信息提取（skip_download=True）、
   不下载媒体、不初始化转写器；miss 路径则完整下载 + 转写 + promote 进缓存。
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import videonote_mcp.server as server  # noqa: F401 —— 触发 setup_environment，隔离数据目录
from app.models.audio_model import AudioDownloadResult
from app.services import note_cache
from app.services.note import NOTE_OUTPUT_DIR, NoteGenerator, pipeline as note_pipeline
from app.services.transcriber_config_manager import TranscriberConfigManager


def _current_engine_key() -> str:
    mgr = TranscriberConfigManager()
    return note_cache.engine_key(mgr.get_transcriber_type(), mgr.get_whisper_model_size())


def _cache_entry(ident: str, key: str, text: str) -> Path:
    p = note_cache.cache_root() / ident / f"transcript_{key}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _fake_downloader(video_id: str, title: str = "T"):
    d = mock.Mock()
    d.download_subtitles.return_value = None  # 平台无字幕，走转写/缓存路径
    d.download.return_value = AudioDownloadResult(
        file_path="", title=title, duration=1.0, cover_url=None,
        platform="youtube", video_id=video_id, raw_info={},
    )
    return d


class IdentityTest(unittest.TestCase):
    def test_bilibili_bv_and_p(self):
        self.assertEqual(
            note_cache.derive_video_id("https://www.bilibili.com/video/BV1xx411c7mD/?p=3", "bilibili"),
            "BV1xx411c7mD:p3",
        )
        self.assertEqual(
            note_cache.derive_video_id("https://www.bilibili.com/video/BV1xx411c7mD", "bilibili"),
            "BV1xx411c7mD",
        )

    def test_youtube_douyin_tiktok(self):
        self.assertEqual(
            note_cache.derive_video_id("https://www.youtube.com/watch?v=abcDEF12345", "youtube"),
            "abcDEF12345",
        )
        self.assertEqual(
            note_cache.derive_video_id("https://youtu.be/abcDEF12345", "youtube"),
            "abcDEF12345",
        )
        self.assertEqual(
            note_cache.derive_video_id("https://www.douyin.com/video/7123456789012345678", "douyin"),
            "7123456789012345678",
        )
        self.assertEqual(
            note_cache.derive_video_id("https://www.tiktok.com/@u/video/7123456789012345678", "tiktok"),
            "7123456789012345678",
        )

    def test_local_sha256_changes_with_content(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "a.mp4"
            f.write_bytes(b"hello")
            v1 = note_cache.derive_video_id(str(f), "local")
            f.write_bytes(b"hello!")
            v2 = note_cache.derive_video_id(str(f), "local")
            self.assertEqual(len(v1), 64)
            self.assertNotEqual(v1, v2)

    def test_unparseable_platform_returns_none(self):
        self.assertIsNone(note_cache.derive_video_id("https://v.kuaishou.com/x", "kuaishou"))
        self.assertIsNone(note_cache.derive_video_id("https://example.com/x", "generic"))

    def test_engine_key_local_pins_size_cloud_does_not(self):
        self.assertEqual(note_cache.engine_key("fast-whisper", "small"), "fast-whisper:small")
        self.assertEqual(note_cache.engine_key("fast-whisper", ""), "fast-whisper")
        self.assertEqual(note_cache.engine_key("groq", "small"), "groq")
        self.assertEqual(note_cache.engine_key("funasr", ""), "funasr")


class CacheRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.root = note_cache.cache_root()
        shutil.rmtree(self.root, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _dest(self, tid: str) -> Path:
        p = NOTE_OUTPUT_DIR / tid / "gen" / "transcript.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def test_miss_when_empty(self):
        dest = self._dest("t1")
        self.assertIsNone(
            note_cache.lookup_transcript(
                "https://www.youtube.com/watch?v=abcDEF12345", "youtube", "fast-whisper", "small", dest
            )
        )
        self.assertFalse(dest.exists())

    def test_hit_copies_to_dest(self):
        _cache_entry("youtube:abcDEF12345", "fast-whisper:small", '{"full_text": "hi", "segments": []}')
        dest = self._dest("t2")
        src = note_cache.lookup_transcript(
            "https://www.youtube.com/watch?v=abcDEF12345", "youtube", "fast-whisper", "small", dest
        )
        self.assertIsNotNone(src)
        self.assertEqual(json.loads(dest.read_text(encoding="utf-8"))["full_text"], "hi")

    def test_engine_change_misses_but_subtitle_is_bonus(self):
        _cache_entry("youtube:abcDEF12345", "fast-whisper:small", "{}")
        dest = self._dest("t3")
        # 换引擎（funasr）→ 不命中 fast-whisper:small，避免误用旧引擎结果
        self.assertIsNone(
            note_cache.lookup_transcript(
                "https://www.youtube.com/watch?v=abcDEF12345", "youtube", "funasr", "", dest
            )
        )
        # 平台字幕键引擎无关：引擎键未命中后作为兜底
        _cache_entry("youtube:abcDEF12345", note_cache.SUBTITLE_KEY, "{}")
        self.assertIsNotNone(
            note_cache.lookup_transcript(
                "https://www.youtube.com/watch?v=abcDEF12345", "youtube", "funasr", "", dest
            )
        )

    def test_promote_then_lookup_hits(self):
        src = self._dest("t4")
        src.write_text(json.dumps({"full_text": "x", "segments": []}), encoding="utf-8")
        note_cache.promote_transcript(
            "youtube", "https://www.youtube.com/watch?v=abcDEF12345", "abcDEF12345",
            "fast-whisper:small", src,
        )
        dest = self._dest("t5")
        self.assertIsNotNone(
            note_cache.lookup_transcript(
                "https://www.youtube.com/watch?v=abcDEF12345", "youtube", "fast-whisper", "small", dest
            )
        )

    def test_bili_multi_p_identity_is_distinct(self):
        _cache_entry("bilibili:BV1xx411c7mD:p2", note_cache.SUBTITLE_KEY, "{}")
        dest = self._dest("t6")
        # p=1 与 p=2 身份不同 → 不互相污染
        self.assertIsNone(
            note_cache.lookup_transcript(
                "https://www.bilibili.com/video/BV1xx411c7mD?p=1", "bilibili", "fast-whisper", "small", dest
            )
        )

    def test_promote_normalizes_bili_audio_video_id(self):
        src = self._dest("t7")
        src.write_text("{}", encoding="utf-8")
        # URL 解析不出 BV（非 b23，避免测试触发真实网络）→ 用下载器权威 video_id 兜底，
        # 且把 `BV…_p2` 后缀归一到缓存身份 `BV…:p2`
        note_cache.promote_transcript(
            "bilibili", "https://www.bilibili.com/video/", "BV1xx411c7mD_p2", note_cache.SUBTITLE_KEY, src
        )
        # 身份是单个冒号组件（platform:video_id），不是嵌套目录
        self.assertTrue(
            (note_cache.cache_root() / "bilibili:BV1xx411c7mD:p2" / "transcript_subtitle.json").exists()
        )

    def test_normalize_bili_video_id(self):
        self.assertEqual(note_cache._normalize_bili_video_id("BV1xx411c7mD"), "BV1xx411c7mD")
        self.assertEqual(note_cache._normalize_bili_video_id("BV1xx411c7mD_p2"), "BV1xx411c7mD:p2")
        self.assertEqual(note_cache._normalize_bili_video_id("x"), "x")


    def test_promote_media_local_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "audio.mp3"
            src.write_bytes(b"audio")
            note_cache.promote_media("local", "/tmp/x.mp4", None, str(src))
        self.assertFalse(any(p.is_dir() and p.name == "media" for p in self.root.rglob("*")))

    def test_promote_then_lookup_media_copies(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "audio.mp3"
            src.write_bytes(b"real-audio-bytes")
            note_cache.promote_media(
                "youtube", "https://www.youtube.com/watch?v=abcDEF12345", "abcDEF12345", str(src)
            )
            media_dir = self.root / "youtube:abcDEF12345" / "media"
            self.assertTrue((media_dir / "audio.mp3").exists())
            dest = NOTE_OUTPUT_DIR / "t10" / "raw"
            copied = note_cache.lookup_media("https://www.youtube.com/watch?v=abcDEF12345", "youtube", dest)
            self.assertIsNotNone(copied)
            self.assertEqual(Path(copied).read_bytes(), b"real-audio-bytes")
            self.assertEqual(Path(copied).parent, dest)

    def test_lookup_media_miss(self):
        dest = NOTE_OUTPUT_DIR / "t11" / "raw"
        self.assertIsNone(note_cache.lookup_media("https://www.youtube.com/watch?v=abcDEF12345", "youtube", dest))

    def test_promote_media_missing_src_noop(self):
        note_cache.promote_media("youtube", "https://www.youtube.com/watch?v=abcDEF12345", "abcDEF12345", "/nonexistent.mp3")
        self.assertFalse(self.root.exists())


class GenerateIntegrationTest(unittest.TestCase):
    YT_URL = "https://www.youtube.com/watch?v=abcDEF12345"

    def setUp(self):
        self.cache = note_cache.cache_root()
        shutil.rmtree(self.cache, ignore_errors=True)
        # 清掉可能残留的同名任务目录（per-task 缓存会短路整条流水线）
        for tid in ("cachehit00001", "cachemiss00001", "media000001", "media000002"):
            shutil.rmtree(NOTE_OUTPUT_DIR / tid, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)
        for tid in ("cachehit00001", "cachemiss00001", "media000001", "media000002"):
            shutil.rmtree(NOTE_OUTPUT_DIR / tid, ignore_errors=True)

    def _gen(self):
        g = NoteGenerator()
        return g

    def test_hit_skips_download_and_transcribe(self):
        key = _current_engine_key()
        _cache_entry("youtube:abcDEF12345", key, json.dumps({
            "language": "zh", "full_text": "缓存转写",
            "segments": [{"start": 0, "end": 1, "text": "缓存转写"}],
        }, ensure_ascii=False))
        gen = self._gen()
        downloader = _fake_downloader("abcDEF12345")
        with mock.patch.object(gen, "_get_downloader", return_value=downloader):
            with mock.patch.object(gen, "_init_transcriber") as init_tr:
                result = gen.generate(
                    video_url=self.YT_URL, platform="youtube",
                    task_id="cachehit00001", material_only=True,
                )
        # 命中 → 只做元信息提取（skip_download=True），不下载媒体、不初始化转写器
        self.assertEqual(downloader.download.call_count, 1)
        self.assertTrue(downloader.download.call_args.kwargs.get("skip_download"))
        downloader.download_video.assert_not_called()
        init_tr.assert_not_called()
        self.assertEqual(result.transcript.full_text, "缓存转写")
        self.assertTrue((NOTE_OUTPUT_DIR / "cachehit00001" / "gen" / "transcript.json").exists())

    def test_miss_downloads_transcribes_and_promotes(self):
        gen = self._gen()
        downloader = _fake_downloader("abcDEF12345")
        transcript_dict = {
            "language": "zh", "full_text": "fresh",
            "segments": [{"start": 0, "end": 1, "text": "fresh"}],
        }
        with mock.patch.object(gen, "_get_downloader", return_value=downloader):
            with mock.patch.object(gen, "_init_transcriber") as init_tr:
                with mock.patch.object(note_pipeline, "fetch_subtitles", return_value=None):
                    with mock.patch.object(note_pipeline, "transcribe_audio", return_value=transcript_dict):
                        result = gen.generate(
                            video_url=self.YT_URL, platform="youtube",
                            task_id="cachemiss00001", material_only=True,
                        )
        # miss → 完整下载 + 转写
        self.assertFalse(downloader.download.call_args.kwargs.get("skip_download"))
        init_tr.assert_called_once()
        self.assertEqual(result.transcript.full_text, "fresh")
        # promote 进缓存：再跑一次就能命中
        key = _current_engine_key()
        entry = note_cache.cache_root() / "youtube:abcDEF12345" / f"transcript_{key}.json"
        self.assertTrue(entry.exists())
        self.assertEqual(json.loads(entry.read_text(encoding="utf-8"))["full_text"], "fresh")

    def test_miss_promotes_media_and_hit_copies_audio(self):
        # run1 完整下载（真实音频文件）→ promote 出媒体缓存；run2 命中 → audio_path 指向
        # run2 raw/ 的真实复制件（不悬空）
        with tempfile.TemporaryDirectory() as td:
            fake_audio = Path(td) / "abcDEF12345.mp3"
            fake_audio.write_bytes(b"fake-mp3")
            downloader = _fake_downloader("abcDEF12345")
            downloader.download.return_value = AudioDownloadResult(
                file_path=str(fake_audio), title="T", duration=1.0, cover_url=None,
                platform="youtube", video_id="abcDEF12345", raw_info={},
            )
            transcript_dict = {
                "language": "zh", "full_text": "fresh",
                "segments": [{"start": 0, "end": 1, "text": "fresh"}],
            }
            gen = self._gen()
            with mock.patch.object(gen, "_get_downloader", return_value=downloader):
                with mock.patch.object(gen, "_init_transcriber"):
                    with mock.patch.object(note_pipeline, "fetch_subtitles", return_value=None):
                        with mock.patch.object(note_pipeline, "transcribe_audio", return_value=transcript_dict):
                            gen.generate(
                                video_url=self.YT_URL, platform="youtube",
                                task_id="media000001", material_only=True,
                            )
            media_dir = self.cache / "youtube:abcDEF12345" / "media"
            self.assertTrue((media_dir / "abcDEF12345.mp3").exists())

            gen2 = self._gen()
            with mock.patch.object(gen2, "_get_downloader", return_value=downloader):
                with mock.patch.object(gen2, "_init_transcriber"):
                    result2 = gen2.generate(
                        video_url=self.YT_URL, platform="youtube",
                        task_id="media000002", material_only=True,
                    )
            ap = result2.material["audio_path"]
            self.assertIsNotNone(ap, "命中缓存后 audio_path 不应悬空")
            self.assertTrue(Path(ap).exists(), f"audio_path 悬空: {ap}")
            self.assertIn("media000002", str(ap))
            self.assertEqual(Path(ap).read_bytes(), b"fake-mp3")


if __name__ == "__main__":
    unittest.main()
