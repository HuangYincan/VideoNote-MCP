"""#142 安全扫描收尾：MCP 本地文件/危险操作边界 + 模型 revision 固定 + EJS 开关。

覆盖点（对应扫描发现 1/2/4/6）：
1. 数据目录外路径门禁（VIDEONOTE_ALLOW_EXTERNAL_PATHS，默认关）：generate_note /
   prepare_note_material 本地视频入口、notes_dir / process_media 输出目录、merge 输入、
   diarize 输入——默认拒绝并指明放行开关；开关放行后回到「只告警不拦截」；
2. 破坏性清理门禁（VIDEONOTE_ALLOW_DESTRUCTIVE_CLEANUP，默认关）：cleanup
   include_config/include_models 拒绝执行（dry_run 如实标注「将拒绝清理」）；
3. 模型 revision 固定：faster-whisper 内置档 / mlx 映射、CLI 下载、运行时加载；
4. youtube VIDEONOTE_YTDLP_EJS 开关（远程组件默认开、可关）；
5. video_reader MD5 usedforsecurity=False（帧去重，非安全用途）。
"""
import json
import sys
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest import mock

# 确保能 import videonote_mcp.server（vendored app.* 在其内部 import）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 必须先 import server：其模块顶层 setup_environment() 会把 NOTE_OUTPUT_DIR 等
# 环境变量设为数据目录的绝对路径（pytest 由 conftest.py 隔离到 /tmp/videonote_pytest）
import videonote_mcp.server as server

_ALLOW_EXT = "VIDEONOTE_ALLOW_EXTERNAL_PATHS"
_DESTRUCTIVE = "VIDEONOTE_ALLOW_DESTRUCTIVE_CLEANUP"


def _outside_tmp_file(suffix: str) -> str:
    """数据目录外的真实文件（tempfile 默认落 /tmp，与测试数据目录隔离）。"""
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    f.close()
    return f.name


class ExternalPathBoundaryTest(unittest.TestCase):
    """#142 A1：数据目录外本地路径默认拒绝，开关放行后回到「只告警」。"""

    def _generate(self, *args, **kw):
        """stub provider/模型/线程池，让 generate_note 干净走到提交点。"""
        done = Future()
        done.set_result(None)
        with mock.patch.object(
            server, "_resolve_default_provider_id", return_value="t-provider"
        ), mock.patch.object(
            server, "get_models_by_provider", return_value=[{"model_name": "t-model"}]
        ), mock.patch("videonote_mcp.server._pool.submit", return_value=done):
            return server.generate_note(*args, **kw)

    def test_generate_note_local_outside_rejected(self):
        path = _outside_tmp_file(".mp4")
        try:
            with self.assertRaises(ValueError) as cm:
                server.generate_note(path)
            self.assertIn(_ALLOW_EXT, str(cm.exception))
            self.assertIn("数据目录内", str(cm.exception))
        finally:
            Path(path).unlink(missing_ok=True)

    def test_generate_note_local_inside_data_dir_submits(self):
        p = server.DATA_DIR / "inside_clip.mp4"
        p.write_bytes(b"x")
        try:
            resp = self._generate(str(p))
            self.assertIn('"status": "PENDING"', resp)  # 边界放行，任务照常提交
        finally:
            p.unlink(missing_ok=True)

    def test_prepare_note_material_local_outside_rejected(self):
        path = _outside_tmp_file(".mp4")
        try:
            with self.assertRaises(ValueError) as cm:
                server.prepare_note_material(path)
            self.assertIn(_ALLOW_EXT, str(cm.exception))
        finally:
            Path(path).unlink(missing_ok=True)

    def test_notes_dir_outside_rejected(self):
        td = tempfile.mkdtemp(prefix="vn_notes_out_")
        try:
            with self.assertRaises(ValueError) as cm:
                self._generate("https://example.com/v", notes_dir=td)
            self.assertIn(_ALLOW_EXT, str(cm.exception))
        finally:
            import shutil

            shutil.rmtree(td, ignore_errors=True)

    def test_notes_dir_inside_data_dir_submits(self):
        resp = self._generate("https://example.com/v", notes_dir=str(server.DATA_DIR))
        self.assertIn('"status": "PENDING"', resp)

    def test_notes_dir_outside_allowed_when_switch_on(self):
        """开关放行 → 回到 #99 的「只告警不拦截」。"""
        td = tempfile.mkdtemp(prefix="vn_notes_allow_")
        try:
            with mock.patch.dict("os.environ", {_ALLOW_EXT: "1"}, clear=False):
                with self.assertLogs("videonote_mcp.server", level="WARNING") as logs:
                    resp = self._generate("https://example.com/v", notes_dir=td)
            self.assertTrue(any("数据目录外" in m for m in logs.output))
            self.assertIn('"status": "PENDING"', resp)
        finally:
            import shutil

            shutil.rmtree(td, ignore_errors=True)

    def test_merge_inputs_outside_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a.mp3", Path(td) / "b.mp3"
            a.write_bytes(b"x")
            b.write_bytes(b"x")
            with mock.patch("app.services.merge.merge_audio") as m:
                resp = json.loads(
                    server.process_media(action="merge", files=[str(a), str(b)])
                )
            self.assertFalse(resp["ok"])
            self.assertIn("合并输入文件", resp["error"])
            m.assert_not_called()

    def test_merge_inputs_outside_allowed_when_switch_on(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a.mp3", Path(td) / "b.mp3"
            a.write_bytes(b"x")
            b.write_bytes(b"x")
            with mock.patch.dict("os.environ", {_ALLOW_EXT: "1"}, clear=False), mock.patch(
                "app.services.merge.merge_audio", return_value=str(Path(td) / "merged.wav")
            ) as m:
                resp = json.loads(
                    server.process_media(action="merge", files=[str(a), str(b)])
                )
            self.assertTrue(resp["ok"])
            m.assert_called_once()

    def test_merge_out_dir_outside_rejected(self):
        # 输入在数据目录内（过输入边界），输出目录再触发边界（先报输入文件再报输出）
        a, b = server.DATA_DIR / "merge_a.mp3", server.DATA_DIR / "merge_b.mp3"
        a.write_bytes(b"x")
        b.write_bytes(b"x")
        td = tempfile.mkdtemp(prefix="vn_merge_out_")
        try:
            with mock.patch("app.services.merge.merge_audio") as m:
                resp = json.loads(
                    server.process_media(action="merge", files=[str(a), str(b)], out_dir=td)
                )
            self.assertFalse(resp["ok"])
            self.assertIn("合并输出目录", resp["error"])
            m.assert_not_called()
        finally:
            import shutil

            shutil.rmtree(td, ignore_errors=True)
            a.unlink(missing_ok=True)
            b.unlink(missing_ok=True)

    def _task_with_transcript(self, tid: str) -> None:
        """造一个 SUCCESS 且有转写的任务（export 分支的前置条件，#126 C1）。"""
        server._atomic_write_json(
            server.NOTE_OUTPUT_DIR / tid / "result.json",
            {
                "transcript": {
                    "language": "zh",
                    "full_text": "hi",
                    "segments": [{"start": 0.0, "end": 1.0, "text": "hi"}],
                }
            },
        )
        server._atomic_write_json(
            server.NOTE_OUTPUT_DIR / tid / "status.json", {"status": "SUCCESS"}
        )

    def _cleanup_task(self, tid: str) -> None:
        import shutil

        shutil.rmtree(server.NOTE_OUTPUT_DIR / tid, ignore_errors=True)

    def test_export_out_dir_outside_rejected(self):
        tid = "extbound14201"
        self._task_with_transcript(tid)
        td = tempfile.mkdtemp(prefix="vn_export_out_")
        try:
            # 与 formats 非法值同口径：入口校验 raise（export 无 try/except 兜底形状）
            with self.assertRaises(ValueError) as cm:
                server.process_media(action="export", task_id=tid, formats=["srt"], out_dir=td)
            self.assertIn("导出输出目录", str(cm.exception))
        finally:
            import shutil

            shutil.rmtree(td, ignore_errors=True)
            self._cleanup_task(tid)

    def test_export_out_dir_outside_allowed_when_switch_on(self):
        tid = "extbound14202"
        self._task_with_transcript(tid)
        td = tempfile.mkdtemp(prefix="vn_export_allow_")
        try:
            with mock.patch.dict("os.environ", {_ALLOW_EXT: "1"}, clear=False):
                resp = json.loads(
                    server.process_media(action="export", task_id=tid, formats=["srt"], out_dir=td)
                )
            self.assertTrue(resp["ok"])
            self.assertTrue((Path(td) / "transcript.srt").exists())
        finally:
            import shutil

            shutil.rmtree(td, ignore_errors=True)
            self._cleanup_task(tid)

    def test_diarize_input_outside_rejected(self):
        path = _outside_tmp_file(".wav")
        try:
            # 不 mock diarize：边界在 normalize 之前触发，报边界错误而非执行分离
            resp = json.loads(server.process_media(action="diarize", audio_file=path))
            self.assertFalse(resp["ok"])
            self.assertIn("说话人分离输入文件", resp["error"])
        finally:
            Path(path).unlink(missing_ok=True)

    def test_symlink_escape_rejected(self):
        """数据目录内软链到目录外 → resolve 跟随符号链接按外部拒绝。"""
        outside = _outside_tmp_file(".mp4")
        link = server.DATA_DIR / "escape_link.mp4"
        try:
            link.symlink_to(outside)
            with self.assertRaises(ValueError):
                server._guard_data_boundary(link, "本地视频路径")
        finally:
            link.unlink(missing_ok=True)
            Path(outside).unlink(missing_ok=True)

    def test_inside_data_dir_helper(self):
        self.assertTrue(server._inside_data_dir(server.DATA_DIR))
        self.assertTrue(server._inside_data_dir(server.DATA_DIR / "note_results"))
        self.assertFalse(server._inside_data_dir(Path(tempfile.gettempdir())))


class DestructiveCleanupGateTest(unittest.TestCase):
    """#142 A1：cleanup include_config/include_models 默认拒绝，env 放行。"""

    def _inject(self, futures):
        old = dict(server._task_futures)
        with server._tasks_lock:
            server._task_futures.clear()
            server._task_futures.update(futures)
        return old

    def _restore(self, old):
        with server._tasks_lock:
            server._task_futures.clear()
            server._task_futures.update(old)

    def test_cleanup_include_config_refused(self):
        old = self._inject({})
        try:
            with mock.patch.object(server, "cleanup_all_files") as m:
                resp = json.loads(server.cleanup(include_config=True))
            self.assertFalse(resp["ok"])
            self.assertIn(_DESTRUCTIVE, resp["error"])
            m.assert_not_called()
        finally:
            self._restore(old)

    def test_cleanup_include_models_refused(self):
        old = self._inject({})
        try:
            with mock.patch.object(server, "cleanup_all_files") as m:
                resp = json.loads(server.cleanup(include_models=True))
            self.assertFalse(resp["ok"])
            self.assertIn(_DESTRUCTIVE, resp["error"])
            m.assert_not_called()
        finally:
            self._restore(old)

    def test_cleanup_plain_still_allowed(self):
        """不带 include_config/include_models 的全局清理不受门禁影响。"""
        old = self._inject({})
        try:
            with mock.patch.object(
                server, "cleanup_all_files", return_value={"cleaned": [], "kept": []}
            ) as m:
                resp = json.loads(server.cleanup())
            self.assertTrue(resp["ok"])
            m.assert_called_once_with(include_config=False, include_models=False)
        finally:
            self._restore(old)

    def test_dry_run_marks_config_will_refuse(self):
        resp = json.loads(
            server.cleanup(include_config=True, include_models=True, dry_run=True)
        )
        self.assertNotIn("config/（LLM key / cookie / 转写设置）", resp["would_clean"])
        self.assertNotIn("models/（已下载模型）", resp["would_clean"])
        self.assertTrue(
            any(_DESTRUCTIVE in k for k in resp["would_keep"]), resp["would_keep"]
        )

    def test_include_models_allowed_when_switch_on(self):
        old = self._inject({})
        try:
            with mock.patch.dict("os.environ", {_DESTRUCTIVE: "1"}, clear=False), mock.patch.object(
                server, "cleanup_all_files", return_value={"cleaned": [], "kept": []}
            ) as m, mock.patch.object(server.dl_state, "downloading_keys", return_value=[]):
                resp = json.loads(server.cleanup(include_models=True))
            self.assertTrue(resp["ok"])
            m.assert_called_once_with(include_config=False, include_models=True)
        finally:
            self._restore(old)

    def test_dry_run_include_flags_clean_when_switch_on(self):
        with mock.patch.dict("os.environ", {_DESTRUCTIVE: "1"}, clear=False):
            resp = json.loads(
                server.cleanup(include_config=True, include_models=True, dry_run=True)
            )
        self.assertIn("config/（LLM key / cookie / 转写设置）", resp["would_clean"])
        self.assertIn("models/（已下载模型）", resp["would_clean"])


class WhisperRevisionPinTest(unittest.TestCase):
    """#142 A2：内置模型固定 revision；自定义/直通不固定；加载与 CLI 下载一致。"""

    def _pinned(self, size: str):
        from app.transcriber.whisper_models import (
            BUILTIN_WHISPER_MODELS,
            BUILTIN_WHISPER_REVISIONS,
            resolve_whisper_revision,
        )

        self.assertEqual(resolve_whisper_revision(size), BUILTIN_WHISPER_REVISIONS[size])
        self.assertEqual(len(BUILTIN_WHISPER_REVISIONS[size]), 40)  # git commit 长度
        self.assertIn(size, BUILTIN_WHISPER_MODELS)  # 每个内置档都有 revision

    def test_all_builtins_pinned(self):
        from app.transcriber.whisper_models import BUILTIN_WHISPER_MODELS

        for size in BUILTIN_WHISPER_MODELS:
            with self.subTest(size=size):
                self._pinned(size)

    def test_custom_and_passthrough_not_pinned(self):
        import tempfile

        from app.transcriber.whisper_models import (
            WhisperModelRegistry,
            resolve_whisper_revision,
        )

        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "whisper_models.json"
            cfg.write_text(json.dumps({"my-model": "acme/whisper-custom"}), encoding="utf-8")
            reg = WhisperModelRegistry(filepath=str(cfg))
            self.assertIsNone(reg.resolve_revision("my-model"))       # 自定义 → 不固定
            self.assertIsNone(reg.resolve_revision("Org/a-repo"))     # 直通 → 不固定
            self.assertEqual(reg.resolve_revision("small"), "536b0662742c02347bc0e980a01041f333bce120")
            self.assertEqual(resolve_whisper_revision("small"), "536b0662742c02347bc0e980a01041f333bce120")

    def test_whisper_load_passes_revision(self):
        from app.transcriber.whisper import WhisperTranscriber

        with mock.patch("app.transcriber.whisper.WhisperModel") as m, mock.patch(
            "app.transcriber.whisper.resolve_whisper_model", return_value="Systran/faster-whisper-small"
        ), mock.patch("app.transcriber.whisper.resolve_whisper_revision", return_value="v1-pinned"):
            tr = WhisperTranscriber.__new__(WhisperTranscriber)
            tr.device = "cpu"
            tr.compute_type = "int8"
            tr._build_model("small", "/tmp/models")
        kwargs = m.call_args.kwargs
        self.assertEqual(kwargs["download_root"], "/tmp/models")
        self.assertEqual(kwargs["revision"], "v1-pinned")

    def test_cli_download_whisper_passes_revision(self):
        from videonote_mcp import cli

        with mock.patch("app.transcriber.whisper_models.resolve_whisper_model", return_value="Systran/faster-whisper-tiny"), \
             mock.patch("app.transcriber.whisper_models.resolve_whisper_revision", return_value="d90ca5fe260221311c53c58e660288d3deb8d356"), \
             mock.patch("app.transcriber.whisper_models.is_local_target", return_value=False), \
             mock.patch("app.utils.path_helper.get_model_dir", return_value="/tmp/models"), \
             mock.patch("huggingface_hub.snapshot_download") as sd, \
             mock.patch("faster_whisper.WhisperModel") as wm:
            cli._download_whisper("tiny")
        self.assertEqual(sd.call_args.kwargs["revision"], "d90ca5fe260221311c53c58e660288d3deb8d356")
        self.assertEqual(wm.call_args.kwargs["revision"], "d90ca5fe260221311c53c58e660288d3deb8d356")

    def test_cli_download_mlx_passes_revision(self):
        from app.utils.model_status import MLX_REPO_REVISIONS
        from videonote_mcp import cli

        with mock.patch("app.utils.path_helper.get_model_dir", return_value="/tmp/mlx"), \
             mock.patch("huggingface_hub.snapshot_download") as sd:
            cli._download_mlx_model("small")
        self.assertEqual(sd.call_args.kwargs["revision"], MLX_REPO_REVISIONS["small"])

    def test_mlx_transcriber_download_pins_revision(self):
        """MLX 构造即下载：platform 桩成 Darwin（不依赖真 macOS，也不 import mlx 框架）。"""
        from app.transcriber import mlx_whisper_transcriber as mw
        from app.utils.model_status import MLX_REPO_REVISIONS

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(mw.platform, "system", return_value="Darwin"), \
             mock.patch.object(mw, "get_model_dir", return_value=td), \
             mock.patch.object(mw, "snapshot_download") as sd:
            mw.MLXWhisperTranscriber("small")
        self.assertEqual(sd.call_args.kwargs["revision"], MLX_REPO_REVISIONS["small"])
        self.assertEqual(sd.call_args.kwargs["local_dir"], str(Path(td) / "mlx-community" / "whisper-small-mlx"))


class YoutubeEjsGateTest(unittest.TestCase):
    """#142 A4：remote_components 默认开（YouTube 硬要求）、VIDEONOTE_YTDLP_EJS=0 可关。"""

    def test_default_enables_remote_components(self):
        from app.downloaders.youtube_downloader import _apply_js_challenge

        opts = _apply_js_challenge({})
        self.assertEqual(opts["remote_components"], ["ejs:github"])
        self.assertIn("node", opts["js_runtimes"])

    def test_env_zero_disables(self):
        from app.downloaders.youtube_downloader import _apply_js_challenge, _ejs_enabled

        with mock.patch.dict("os.environ", {"VIDEONOTE_YTDLP_EJS": "0"}, clear=False):
            self.assertFalse(_ejs_enabled())
            opts = _apply_js_challenge({})
        self.assertNotIn("remote_components", opts)
        self.assertNotIn("js_runtimes", opts)

    def test_junk_env_falls_back_on(self):
        from app.downloaders.youtube_downloader import _ejs_enabled

        with mock.patch.dict("os.environ", {"VIDEONOTE_YTDLP_EJS": "maybe"}, clear=False):
            self.assertTrue(_ejs_enabled())  # 垃圾值回退默认（env_bool 语义）
        self.assertTrue(_ejs_enabled())  # 未设置也是默认开


class Md5DedupTest(unittest.TestCase):
    """#142 A6：帧去重 MD5 加 usedforsecurity=False（FIPS 兼容 + 消除「MD5 用于安全」误读）。"""

    def test_md5_digest_unchanged(self):
        import hashlib

        from app.utils.video_reader import VideoReader

        path = _outside_tmp_file(".bin")
        try:
            Path(path).write_bytes(b"frame-bytes\x00\xff")
            self.assertEqual(
                VideoReader._calculate_file_md5(path),
                hashlib.md5(b"frame-bytes\x00\xff", usedforsecurity=False).hexdigest(),
            )
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
