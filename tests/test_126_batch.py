"""#126 批测试：第 10 轮全库扫描 18 项修复的回归覆盖（C1-C9 + B1-B9）。

第 10 轮双 agent 扫描结论：无 P0，剩余问题呈「半途修复收尾未闭环」模式
（gate 只覆盖一半 / close 从未调用 / 异常静默吞掉）+ 长尾边界 + CLI/MCP 不对称。
本文件逐项验证 #126 的修复行为。不碰真实网络，requests / yt-dlp / pyannote 全 mock。

运行：
    cd <repo>
    .venv/bin/python tests/test_126_batch.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 与会话级 conftest 同库（全量 pytest 时 conftest 已设 DATABASE_URL，这里 setdefault 不覆盖）；
# 直接 `python tests/test_126_batch.py` 时 conftest 不加载，setdefault 兜底自建同路径库。
os.environ.setdefault(
    "DATABASE_URL", "sqlite:////tmp/videonote_pytest/video_note.db"
)

import videonote_mcp.server as server  # noqa: E402


def _make_task(task_id: str, status: str = "SUCCESS") -> Path:
    """在隔离输出目录造任务：status.json + gen/transcript.json + result.json。"""
    task_dir = server.NOTE_OUTPUT_DIR / task_id
    (task_dir / "gen").mkdir(parents=True, exist_ok=True)
    (task_dir / "status.json").write_text(
        json.dumps({"status": status, "message": "状态"}, ensure_ascii=False),
        encoding="utf-8",
    )
    transcript = {
        "language": "zh",
        "full_text": "第一句 第二句",
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "第一句"},
            {"start": 1.0, "end": 2.0, "text": "第二句"},
        ],
    }
    (task_dir / "gen" / "transcript.json").write_text(
        json.dumps(transcript, ensure_ascii=False), encoding="utf-8"
    )
    (task_dir / "result.json").write_text(
        json.dumps({"title": "t", "transcript": transcript}, ensure_ascii=False),
        encoding="utf-8",
    )
    return task_dir


# ---------------- C1：export_transcript SUCCESS 门禁 ----------------

class ExportTranscriptGateTest(unittest.TestCase):
    """C1：非 SUCCESS 任务即使有转写缓存也拒绝导出（与 get_task_transcript 同口径）。"""

    def setUp(self):
        self.tid = "c1export0001"
        self.tdir = _make_task(self.tid)

    def tearDown(self):
        shutil.rmtree(self.tdir, ignore_errors=True)

    def test_success_task_exports(self):
        resp = json.loads(server.process_media(action="export", task_id=self.tid, formats=["srt"]))
        self.assertTrue(resp["ok"])
        srt = resp["formats"]["srt"]
        self.assertTrue(Path(srt.removeprefix("file://")).exists())

    def test_failed_task_rejected_even_with_transcript(self):
        # 转写缓存已落盘，但任务 FAILED——不设门禁会导出成功（#126 C1）
        (self.tdir / "status.json").write_text(
            json.dumps({"status": "FAILED", "message": "失败"}, ensure_ascii=False),
            encoding="utf-8",
        )
        resp = json.loads(server.process_media(action="export", task_id=self.tid, formats=["srt"]))
        self.assertFalse(resp["ok"])
        self.assertIn("任务未成功", resp["error"])

    def test_running_task_rejected(self):
        (self.tdir / "status.json").write_text(
            json.dumps({"status": "TRANSCRIBING", "message": "转写中"}, ensure_ascii=False),
            encoding="utf-8",
        )
        resp = json.loads(server.process_media(action="export", task_id=self.tid, formats=["srt"]))
        self.assertFalse(resp["ok"])
        self.assertIn("TRANSCRIBING", resp["error"])


# ---------------- C2：cleanup_all 成功路径带 ok:true ----------------

class CleanupAllOkTest(unittest.TestCase):
    """C2：成功路径返回 {ok:true}，与拒绝路径 {ok:false} 对称。"""

    def test_cleanup_all_returns_ok_true(self):
        tid = "c2cleanup001"
        tdir = _make_task(tid)
        marker = tdir / "raw"
        marker.mkdir(exist_ok=True)
        (marker / "video.mp4").write_bytes(b"x")
        try:
            resp = json.loads(server.cleanup())
            self.assertTrue(resp["ok"])
            self.assertIn("note_results", resp["cleaned"])
            self.assertFalse(marker.exists())
            # 默认保留 config/ 与 models/（设计红线）
            self.assertIn("config", resp["kept"])
            self.assertIn("models", resp["kept"])
        finally:
            shutil.rmtree(tdir, ignore_errors=True)


# ---------------- C3：fetch_comments limit 垃圾值回退 ----------------

class AppConfigPerKeyTest(unittest.TestCase):
    """C6：手改 JSON 把敏感字段写成非字符串（123）→ 只 drop 该键，其余保留。"""

    def setUp(self):
        from videonote_mcp.config import get_app_config  # noqa: F401 —— 触发环境初始化

        self.cfg_dir = Path(os.environ["VIDEONOTE_CONFIG_DIR"])
        self.cfg_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.cfg_dir / "app_config.json"
        self.saved = None
        if self.path.exists():
            self.saved = self.path.read_text(encoding="utf-8")
        self.path.write_text(
            json.dumps(
                {
                    "hf_token": 123,  # 手滑写成非字符串
                    "notes_dir": "/tmp/my_notes",
                    "default_model": {"provider_id": "p1", "id": "m1"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        if self.saved is None:
            self.path.unlink(missing_ok=True)
        else:
            self.path.write_text(self.saved, encoding="utf-8")

    def test_bad_sensitive_value_keeps_other_keys(self):
        from videonote_mcp.config import get_app_config

        cfg = get_app_config()
        self.assertNotIn("hf_token", cfg)
        self.assertEqual(cfg["notes_dir"], "/tmp/my_notes")
        self.assertEqual(cfg["default_model"]["id"], "m1")


# ---------------- C8：CLI export --out-dir file:// 规整 ----------------

class CliExportFileUriTest(unittest.TestCase):
    """C8：CLI `export --out-dir file://…` 写入真实路径，不建字面 `file:` 目录。"""

    def setUp(self):
        self.tid = "c8cliexp001"
        self.tdir = _make_task(self.tid)
        self.out = tempfile.mkdtemp(prefix="vn_c8_out_")

    def tearDown(self):
        shutil.rmtree(self.tdir, ignore_errors=True)
        shutil.rmtree(self.out, ignore_errors=True)

    def test_file_uri_out_dir_writes_real_path(self):
        from videonote_mcp import cli

        uri = f"file://{self.out}/字幕 导出"  # 含空格/中文 → 验证 unquote 路径
        with mock.patch("builtins.print"), mock.patch.object(cli.sys, "exit"):
            cli._export_cli(["export", self.tid, "--out-dir", uri])
        # 真实路径写入（file:// 解出的 unquote 路径；文件名与 exporter 约定一致）
        srt = Path(self.out) / "字幕 导出" / "transcript.srt"
        self.assertTrue(srt.exists(), f"srt 未写入: {srt}")
        # 没有出现字面 `file:` 目录
        self.assertFalse((Path(self.out) / "file:").exists())

    def test_failed_task_rejected_by_cli_gate(self):
        """C1 另一半：CLI export 对非 SUCCESS 任务同样拒绝（与 MCP 同口径）。"""
        from videonote_mcp import cli

        (self.tdir / "status.json").write_text(
            json.dumps({"status": "FAILED", "message": "失败"}, ensure_ascii=False),
            encoding="utf-8",
        )
        with mock.patch("builtins.print") as m_print:
            with self.assertRaises(SystemExit) as cm:
                cli._export_cli(["export", self.tid, "--out-dir", self.out])
        self.assertEqual(cm.exception.code, 1)
        self.assertTrue(
            any("任务未成功" in c[0][0] for c in m_print.call_args_list),
            f"未打印拒绝消息: {m_print.call_args_list}",
        )
        # 拒绝路径不落盘
        self.assertFalse(list(Path(self.out).glob("transcript.*")))


# ---------------- C9：inspect_video 本地路径存在性前置校验（#136 由 validate_url 并入） ----------------

class InspectVideoLocalTest(unittest.TestCase):
    """C9：本地路径不存在 → ok:false + 明确 error（SKILL 流程少一轮无效往返）。"""

    def test_missing_local_file_rejected(self):
        resp = json.loads(server.inspect_video("/nonexistent/vn_test/missing.mp4"))
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["platform"], "local")
        self.assertIn("本地文件不存在", resp["error"])

    def test_existing_local_file_accepted(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            path = f.name
        try:
            resp = json.loads(server.inspect_video(path))
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["platform"], "local")
            self.assertEqual(resp["kind"], "single")
        finally:
            os.unlink(path)


# ---------------- B1：pipeline 预处理/说话人分离临时目录隔离 ----------------

class PipelineTempDirIsolationTest(unittest.TestCase):
    """B1：prep（vn_prep_）/ 说话人分离（vn_dia_）产物落独立 mkdtemp，
    并发任务处理同一源文件互不覆盖/误删；清理只删自己创建的目录。"""

    def _fake_normalize(self, captured, wav_bytes=b"fakewav"):
        def _fn(input_path, out_dir=None):
            captured["out_dir"] = str(out_dir)
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            wav = str(Path(out_dir) / "src_16k.wav")
            Path(wav).write_bytes(wav_bytes)
            return wav

        return _fn

    def test_apply_diarization_isolated_temp_dir(self):
        from app.models.transcriber_model import TranscriptSegment
        from app.services import pipeline

        seg = TranscriptSegment(start=0.0, end=1.0, text="你好")
        captured = {}

        with (
            mock.patch.object(
                server.TranscriberConfigManager, "get_diarization", return_value=True
            ),
            mock.patch.object(
                server.TranscriberConfigManager, "get_diarization_speakers", return_value=None
            ),
            mock.patch(
                "app.services.diarization.diarize_audio",
                side_effect=lambda wav, num_speakers=None: [
                    {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}
                ],
            ),
            mock.patch(
                "app.services.diarization.assign_speakers",
                side_effect=lambda segs, turns: segs,
            ),
            mock.patch(
                "app.transcriber.audio_preprocess.normalize_to_wav",
                side_effect=self._fake_normalize(captured),
            ),
        ):
            result = pipeline.apply_diarization("/tmp/src.mp4", [seg])
            prep_dir = captured["out_dir"]

        self.assertEqual(len(result), 1)
        # 自建 wav 落独立 vn_dia_ 目录（不再源文件同目录 <名>_16k.wav）
        self.assertTrue(prep_dir.startswith(tempfile.gettempdir()), prep_dir)
        self.assertIn("vn_dia_", prep_dir)
        # 调用后目录已被清理
        self.assertFalse(Path(prep_dir).exists())

    def test_transcribe_with_preprocess_isolated_and_reuses_wav(self):
        from app.models.transcriber_model import TranscriptResult, TranscriptSegment
        from app.services import pipeline

        captured = {}
        wav_in_prep = {}

        class FakeTranscriber:
            def transcript(self, file_path):
                return TranscriptResult(
                    language="zh",
                    full_text="你好",
                    segments=[TranscriptSegment(start=0.0, end=1.0, text="你好")],
                )

        def _fake_diarization(audio_file, segments, wav_path=None):
            wav_in_prep["wav_path"] = wav_path
            return segments

        with (
            mock.patch(
                "app.transcriber.audio_preprocess.normalize_to_wav",
                side_effect=self._fake_normalize(captured),
            ),
            mock.patch(
                "app.transcriber.audio_preprocess.chunk_if_long",
                side_effect=lambda wav, max_seconds: [wav],
            ),
            mock.patch(
                "app.services.pipeline.apply_diarization",
                side_effect=_fake_diarization,
            ),
        ):
            result = pipeline._transcribe_with_preprocess("/tmp/src.mp4", FakeTranscriber())

        prep_dir = captured["out_dir"]
        self.assertTrue(prep_dir.startswith(tempfile.gettempdir()), prep_dir)
        self.assertIn("vn_prep_", prep_dir)
        # diarization 复用 prep 的 wav（created=False，不再二次 mkdtemp）
        self.assertIsNotNone(wav_in_prep["wav_path"])
        self.assertEqual(Path(wav_in_prep["wav_path"]).parent, Path(prep_dir))
        # 调用后整个 prep 目录被清理
        self.assertFalse(Path(prep_dir).exists())
        self.assertEqual(result["full_text"], "你好")
        self.assertEqual(result["language"], "zh")


# ---------------- B2：youtube 下载器 finally close ----------------

class YoutubeDownloaderCloseTest(unittest.TestCase):
    """B2：download_subtitles 成功/异常路径都调用 fetcher.close()（连接池不泄漏）。"""

    URL = "https://www.youtube.com/watch?v=abcdefghijk"

    def test_success_path_closes_fetcher(self):
        from app.downloaders.youtube_downloader import YoutubeDownloader

        with mock.patch(
            "app.downloaders.youtube_downloader.YouTubeSubtitleFetcher"
        ) as m_cls:
            m_cls.return_value.fetch_subtitles.return_value = mock.Mock()
            YoutubeDownloader().download_subtitles(self.URL)
        m_cls.return_value.close.assert_called_once()

    def test_error_path_still_closes_fetcher(self):
        from app.downloaders.youtube_downloader import YoutubeDownloader

        with mock.patch(
            "app.downloaders.youtube_downloader.YouTubeSubtitleFetcher"
        ) as m_cls:
            m_cls.return_value.fetch_subtitles.side_effect = ConnectionError("boom")
            with self.assertRaises(ConnectionError):
                YoutubeDownloader().download_subtitles(self.URL)
        m_cls.return_value.close.assert_called_once()


# ---------------- B3：dm patch 旧版 yt-dlp fatal 降级 ----------------

class DmPatchFatalFallbackTest(unittest.TestCase):
    """B3：yt-dlp 版本无 fatal kwarg 时 TypeError 降级重调，不挂掉所有 B 站下载。"""

    def test_fatal_typeerror_falls_back_without_fatal(self):
        import yt_dlp.extractor.bilibili as bili_mod

        from app.downloaders import bilibili_dm_patch

        fake_ie = mock.Mock()
        orig = mock.Mock()
        orig._bili_dm_patched = False
        # 旧版 yt-dlp：签名 (self, bvid, cid, headers, query) 无 fatal
        orig.side_effect = [TypeError(), mock.Mock()]

        with mock.patch.object(bili_mod, "BilibiliBaseIE", fake_ie):
            fake_ie._download_playinfo = orig
            bilibili_dm_patch.apply_bilibili_dm_img_patch()
            patched = fake_ie._download_playinfo
            result = patched("fake_self", bvid="bv1", cid="cid1", query={}, fatal=True)

        # 第一次调用带 fatal（新签名）→ TypeError；第二次无 fatal（旧签名）→ 成功
        self.assertEqual(orig.call_count, 2)
        first, second = orig.call_args_list
        self.assertTrue(first.kwargs["fatal"])
        self.assertNotIn("fatal", second.kwargs)
        self.assertIsNotNone(result)

    def test_new_signature_fatal_passthrough(self):
        import yt_dlp.extractor.bilibili as bili_mod

        from app.downloaders import bilibili_dm_patch

        fake_ie = mock.Mock()
        orig = mock.Mock()
        orig._bili_dm_patched = False
        orig.side_effect = [mock.Mock()]  # 新版：一次成功

        with mock.patch.object(bili_mod, "BilibiliBaseIE", fake_ie):
            fake_ie._download_playinfo = orig
            bilibili_dm_patch.apply_bilibili_dm_img_patch()
            fake_ie._download_playinfo("fake_self", bvid="bv1", cid="cid1", query={}, fatal=True)

        self.assertEqual(orig.call_count, 1)
        self.assertTrue(orig.call_args.kwargs["fatal"])


# ---------------- B4：short URL 只解析一次（lru_cache） ----------------

class UrlParserCacheTest(unittest.TestCase):
    """B4：短链 resolve 走 lru_cache，同一短链只发一次网络请求。

    #140：resolve 改走 url_safety.public_head（逐跳校验）——requests.head
    不再被调用，mock 点下沉到 adapter（出站最后一层，挡住真实 I/O）。
    """

    @staticmethod
    def _ok_resp(url: str):
        import requests

        resp = requests.Response()
        resp.status_code = 200
        resp._content = b""
        resp.url = url
        resp.request = requests.Request("HEAD", url).prepare()
        resp.raw = None
        resp.headers = requests.structures.CaseInsensitiveDict()
        return resp

    def test_bilibili_short_url_resolved_once(self):
        from app.utils import url_parser

        url = "https://b23.tv/cachetest0001"
        calls = []

        def _send(request, **kwargs):
            calls.append(request.url)
            return self._ok_resp("https://www.bilibili.com/video/BV1xx411c7mD")

        with mock.patch("requests.adapters.HTTPAdapter.send", side_effect=_send):
            url_parser.resolve_bilibili_short_url(url)
            url_parser.resolve_bilibili_short_url(url)
        self.assertEqual(len(calls), 1)

    def test_douyin_short_url_resolved_once(self):
        from app.utils import url_parser

        url = "https://v.douyin.com/cachetest0002/"
        calls = []

        def _send(request, **kwargs):
            calls.append(request.url)
            return self._ok_resp("https://www.douyin.com/video/1234567890123456789")

        with mock.patch("requests.adapters.HTTPAdapter.send", side_effect=_send):
            url_parser.resolve_douyin_short_url(url)
            url_parser.resolve_douyin_short_url(url)
        self.assertEqual(len(calls), 1)


# ---------------- B5：bcut null 文案 / 异常分片参数 ----------------

class BcutNullAndPerSizeTest(unittest.TestCase):
    """B5：必剪 API 返回 null 文案不裸崩；per_size=0 时前置报错而非晦涩 HTTP 400。"""

    def test_null_transcript_text_stripped_to_empty(self):
        from app.transcriber.bcut import BcutTranscriber

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(b"fakemp3data")
            audio = f.name
        try:
            t = BcutTranscriber()
            session = mock.Mock()
            t.session = session

            # 申请上传 → 提交上传 → 创建任务 → 查询结果（state=4 完成）
            session.post.side_effect = [
                _bcut_ok({"in_boss_key": "ik", "resource_id": "r", "upload_id": "u",
                          "upload_urls": ["http://u/0"], "per_size": 1024, "size": 100}),
                _bcut_ok({"download_url": "http://d"}),
                _bcut_ok({"task_id": "t1"}),
            ]
            session.put.return_value = mock.Mock(headers={"Etag": '"etag1"'})
            session.get.return_value = _bcut_ok({
                "state": 4,
                "result": json.dumps({
                    "language": "zh",
                    "utterances": [
                        {"transcript": None, "start_time": 0, "end_time": 1000},
                        {"transcript": " 你好 ", "start_time": 1000, "end_time": 2000},
                    ],
                }),
            })
            try:
                result = t.transcript(audio)
            finally:
                t.close()
            self.assertEqual(result.segments[0].text, "")  # None → 空串，不 AttributeError
            self.assertEqual(result.segments[1].text, "你好")
        finally:
            os.unlink(audio)

    def test_per_size_zero_raises_before_upload(self):
        from app.transcriber.bcut import BcutTranscriber

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(b"fakemp3data")
            audio = f.name
        try:
            t = BcutTranscriber()
            session = mock.Mock()
            t.session = session
            # 服务端返回异常分片参数：per_size=0 且 upload_urls 为空
            session.post.return_value = _bcut_ok({
                "in_boss_key": "ik", "resource_id": "r", "upload_id": "u",
                "upload_urls": [], "per_size": 0, "size": 100,
            })
            try:
                with self.assertRaises(RuntimeError) as cm:
                    t.transcript(audio)
                self.assertIn("异常分片参数", str(cm.exception))
                session.put.assert_not_called()  # 不上传空块
            finally:
                t.close()
        finally:
            os.unlink(audio)


def _bcut_ok(data):
    resp = mock.Mock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"code": 0, "data": data}
    return resp


# ---------------- B7：cleanup_all 索引清空失败不静默 ----------------

class CleanupAllIndexErrorTest(unittest.TestCase):
    """B7：delete_all_tasks 失败 → warning + index_error 字段（目录清了索引残留有痕迹）。"""

    def test_index_failure_surfaces_index_error(self):
        from app.utils import task_manifest

        with mock.patch(
            "app.db.video_task_dao.delete_all_tasks", side_effect=RuntimeError("db locked")
        ):
            result = task_manifest.cleanup_all_files()
        self.assertIn("index_error", result)
        self.assertEqual(result["index_error"], "db locked")
        # 目录清理照常进行，config/models 照常保留
        self.assertIn("note_results", result["cleaned"])
        self.assertIn("config", result["kept"])
        self.assertIn("models", result["kept"])


# ---------------- B8：DAO title=None 不误报插入失败 ----------------

class DaoTitleNoneTest(unittest.TestCase):
    """B8：title=None（无标题视频）入库不抛错、f-string 不 TypeError（#126 B8）。"""

    @classmethod
    def setUpClass(cls):
        from app.db.init_db import init_db

        init_db()

    def test_insert_with_none_title(self):
        from app.db.video_task_dao import insert_video_task, list_tasks

        task_id = "b8title00001"
        insert_video_task(
            video_id="BV1xx", platform="bilibili", task_id=task_id, title=None
        )
        rows = [r for r in list_tasks() if r["task_id"] == task_id]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "")  # 入库成功，title 降级空串


# ---------------- B9：update_config 并发 RMW 不丢字段 ----------------

class UpdateConfigConcurrentTest(unittest.TestCase):
    """B9：并发 update_config 各自字段都保留（RLock 包住 read-modify-write）。"""

    def test_concurrent_updates_keep_both_fields(self):
        from app.services.transcriber_config_manager import TranscriberConfigManager

        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "transcriber.json")
            mgr = TranscriberConfigManager(filepath=path)
            barrier = threading.Barrier(2)
            errors = []

            def _set_preprocess():
                try:
                    barrier.wait()
                    for _ in range(20):
                        mgr.update_config("fast-whisper", enable_preprocess=True)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            def _set_diarization():
                try:
                    barrier.wait()
                    for _ in range(20):
                        mgr.update_config("fast-whisper", diarization=True)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            t1 = threading.Thread(target=_set_preprocess)
            t2 = threading.Thread(target=_set_diarization)
            t1.start()
            t2.start()
            t1.join(timeout=30)
            t2.join(timeout=30)

            self.assertFalse(errors)
            cfg = mgr.get_config()
            # 无锁时后写者覆盖先写者，其中一个字段必丢
            self.assertTrue(cfg["enable_preprocess"])
            self.assertTrue(cfg["diarization"])


# ---------------- #127 A1：note/material 提交即入全局索引 ----------------

class NoteTaskIndexingTest(unittest.TestCase):
    """#127 A1：generate_note / prepare_note_material 提交时即入索引——
    运行期每次 _write_status 不再刷「不在全局索引」warning，FAILED 任务也可见。"""

    def test_generate_note_indexes_on_submit(self):
        done = mock.Mock()
        with mock.patch(
            "videonote_mcp.server._resolve_default_provider_id", return_value="t-provider"
        ), mock.patch(
            "videonote_mcp.server.get_models_by_provider", return_value=[{"model_name": "t-model"}]
        ), mock.patch("videonote_mcp.server._pool.submit", return_value=done):
            with mock.patch("videonote_mcp.server._index_step_task") as m_idx:
                server.generate_note("https://example.com/v")
        m_idx.assert_called_once()
        # example.com 未匹配内置平台 → generic
        self.assertEqual(m_idx.call_args.args[1], "generic")

    def test_prepare_material_indexes_on_submit(self):
        done = mock.Mock()
        with mock.patch("videonote_mcp.server._pool.submit", return_value=done):
            with mock.patch("videonote_mcp.server._index_step_task") as m_idx:
                server.prepare_note_material("https://example.com/v")
        m_idx.assert_called_once()
        self.assertEqual(m_idx.call_args.args[1], "generic")


# ---------------- #127 A3：export 转写来源 gen 优先 ----------------

class ExportGenPriorityTest(unittest.TestCase):
    """#127 A3：gen/transcript.json 是规范来源（#122 A2），result.json 兜底——
    server export 与 CLI / _load_task_transcript 同口径。"""

    def test_gen_wins_when_both_present(self):
        tid = "c1export_genpri"
        tdir = server.NOTE_OUTPUT_DIR / tid
        (tdir / "gen").mkdir(parents=True, exist_ok=True)
        (tdir / "status.json").write_text(
            json.dumps({"status": "SUCCESS"}, ensure_ascii=False), encoding="utf-8"
        )
        gen_tr = {
            "language": "zh",
            "full_text": "来自 gen 规范来源",
            "segments": [{"start": 0.0, "end": 1.0, "text": "来自 gen 规范来源"}],
        }
        result_tr = {
            "language": "zh",
            "full_text": "来自 result 兜底",
            "segments": [{"start": 0.0, "end": 1.0, "text": "来自 result 兜底"}],
        }
        (tdir / "gen" / "transcript.json").write_text(
            json.dumps(gen_tr, ensure_ascii=False), encoding="utf-8"
        )
        (tdir / "result.json").write_text(
            json.dumps({"title": "t", "transcript": result_tr}, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            out_dir = tdir / "export_out"
            resp = json.loads(server.process_media(action="export", task_id=tid, formats=["srt"], out_dir=str(out_dir)))
            self.assertTrue(resp["ok"])
            self.assertIn("来自 gen", (out_dir / "transcript.srt").read_text(encoding="utf-8"))
        finally:
            shutil.rmtree(tdir, ignore_errors=True)


# ---------------- #127 A5：inspect_video 失败形状（#136 由 validate_url 并入） ----------------

class InspectVideoShapeTest(unittest.TestCase):
    """#127 A5：失败分支形状对称——空 url / 本地缺失都带可判读字段。"""

    def test_empty_url_rejected(self):
        resp = json.loads(server.inspect_video(""))
        self.assertFalse(resp["ok"])
        self.assertTrue(resp["error"])

    def test_local_missing_has_platform(self):
        resp = json.loads(server.inspect_video("/nonexistent/nope_12345.mp4"))
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["platform"], "local")


# ---------------- #127 A7 / #136：cleanup_note dry_run（并入 get_task_files） ----------------

class CleanupNoteDryRunTest(unittest.TestCase):
    """#127 A7 / #136：dry_run=True 只列出不删——ok 形状与 manifest/existing/meta 齐备，磁盘不变。"""

    def test_dry_run_lists_and_does_not_delete(self):
        tid = "c1files_ok01"
        tdir = _make_task(tid)
        try:
            resp = json.loads(server.cleanup(tid, dry_run=True))
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["task_id"], tid)
            self.assertTrue(resp["dry_run"])
            self.assertTrue(resp["existing"])
            # 不删任何东西：任务文件夹原样保留
            self.assertTrue(tdir.exists())
            self.assertTrue((tdir / "gen" / "transcript.json").exists())
        finally:
            shutil.rmtree(tdir, ignore_errors=True)


# ---------------- #127 A8：get_task_transcript 切片分隔符一致 ----------------

class TranscriptSliceSeparatorTest(unittest.TestCase):
    """#127 A8：切片路径 full_text 与全量（缓存空格分隔）同分隔符，
    Agent 对比「all」与「0-N」字节数时不失真。"""

    def test_slice_uses_space_join(self):
        tid = "c1slice_sep01"
        tdir = _make_task(tid)
        try:
            resp = json.loads(server.task(tid, action="transcript", segment_range="0-1"))
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["full_text"], "第一句")
            # 全量与切片同分隔符：全量是「第一句 第二句」（空格分隔），切片 0-1 只含前段
            full = json.loads(server.task(tid, action="transcript", segment_range="all"))
            self.assertEqual(full["full_text"], "第一句 第二句")
        finally:
            shutil.rmtree(tdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
