"""#127 批测试：第 11 轮全库扫描 18 项修复的回归覆盖（A1-A8 + B1-B10）。

第 11 轮双 agent 扫描结论：无 P0，残余画像仍为「半途修复收口未闭环」的漏网兄弟
（step 入索引了 note 没入、add_model 校验了 delete_model 没校验、bcut 改分片读了
kuaishou 没改）+ 长尾边界。A 组契约测试在 test_126_batch.py，本文件覆盖 B 组 app 层
修复与 A 组缺的 app 侧行为。不碰真实网络 / 转写引擎 / LLM，全 mock。

运行：
    cd <repo>
    .venv/bin/python tests/test_127_batch.py
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 与会话级 conftest 同库（全量 pytest 时 conftest 已设 DATABASE_URL，这里 setdefault 不覆盖）；
# 直接 `python tests/test_127_batch.py` 时 conftest 不加载，setdefault 兜底自建同路径库。
os.environ.setdefault(
    "DATABASE_URL", "sqlite:////tmp/videonote_pytest/video_note.db"
)


# ---------------- B1：FunASR cuda 探测回退 ----------------

class FunASRDeviceTest(unittest.TestCase):
    """#127 B1：请求 cuda 但探测不到 → 回退 cpu（无 CUDA 机器构造不再崩）。"""

    def test_cuda_falls_back_to_cpu(self):
        from app.transcriber.funasr_transcriber import FunASRTranscriber

        with mock.patch("app.utils.env_checker.is_cuda_available", return_value=False):
            tr = FunASRTranscriber(device="cuda")
        self.assertEqual(tr.device, "cpu")

    def test_cpu_passthrough(self):
        from app.transcriber.funasr_transcriber import FunASRTranscriber

        tr = FunASRTranscriber(device="cpu")
        self.assertEqual(tr.device, "cpu")

    def test_has_close(self):
        from app.transcriber.funasr_transcriber import FunASRTranscriber

        tr = FunASRTranscriber()
        self.assertTrue(callable(tr.close))
        tr.close()  # 不崩


# ---------------- B3：whisper 家族 close() ----------------

class TranscriberCloseTest(unittest.TestCase):
    """#127 B3：whisper 家族实现 close()，transcriber_provider 防御性释放不再静默 no-op。"""

    def test_whisper_close_releases_model(self):
        from app.transcriber.whisper import WhisperTranscriber

        tr = object.__new__(WhisperTranscriber)  # 不构造（会加载模型）
        tr.model = object()
        tr.close()
        self.assertIsNone(tr.model)

    def test_mlx_close_noop(self):
        from app.transcriber.mlx_whisper_transcriber import MLXWhisperTranscriber

        tr = object.__new__(MLXWhisperTranscriber)
        tr.model_path = "/tmp/x"
        tr.close()
        self.assertIsNone(tr.model_path)

    def test_provider_calls_close_on_rebuild(self):
        """transcriber_provider 重建时真的调旧实例 close()。"""
        from app.transcriber import transcriber_provider as tp

        old = mock.Mock()
        old.close = mock.Mock()
        old.model_size = "tiny"
        key = tp.TranscriberType.FAST_WHISPER
        saved = tp._transcribers[key]
        tp._transcribers[key] = old
        try:
            cls = mock.Mock(return_value=mock.Mock())
            cls.__name__ = "FakeTranscriber"  # logger 里访问 cls.__name__
            tp._get_or_build_transcriber(key, cls, model_size="small")
            old.close.assert_called_once()
        finally:
            tp._transcribers[key] = saved


# ---------------- B4：kuaishou 流式上传 ----------------

class KuaishouStreamingUploadTest(unittest.TestCase):
    """#127 B4：提交传文件对象而非整文件 bytes（长音频不再整读进内存）。"""

    def test_submit_passes_file_object(self):
        from app.transcriber.kuaishou import KuaishouTranscriber

        tr = KuaishouTranscriber()
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.write(fd, b"x" * 1024)
        os.close(fd)
        try:
            with mock.patch("app.transcriber.kuaishou.requests.post") as m_post:
                resp = mock.Mock()
                resp.raise_for_status.return_value = None
                resp.json.return_value = {"code": 0, "data": {"text": []}}
                m_post.return_value = resp
                tr._submit(path)
            files = m_post.call_args.kwargs["files"]
            self.assertEqual(len(files), 1)
            fileobj = files[0][1][1]
            # 是文件对象（可 read），不是一次性读入的 bytes
            self.assertTrue(hasattr(fileobj, "read"))
            self.assertIsInstance(fileobj, type(open(path, "rb")))
        finally:
            os.unlink(path)


# ---------------- B5：kuaishou caption None ----------------

class KuaishouCaptionNoneTest(unittest.TestCase):
    """#127 B5：GraphQL 返回 caption:null 时不 AttributeError。"""

    def test_null_caption_no_crash(self):
        from app.downloaders.kuaishou_downloader import KuaiShouDownloader

        d = KuaiShouDownloader()
        with mock.patch("app.downloaders.kuaishou_downloader.KuaiShou") as m_ks:
            m_ks.return_value.run.return_value = {
                "visionVideoDetail": {"photo": {"id": "v1", "caption": None, "duration": 10, "coverUrl": ""}},
                "tags": [],
            }
            with mock.patch("app.downloaders.kuaishou_downloader.os.makedirs"):
                result = d.download("https://v.kuaishou.com/x", skip_download=True)
        # None caption → 空 title，不裸崩
        self.assertEqual(result.title, "")
        self.assertEqual(result.video_id, "v1")


# ---------------- B6：douyin download_video 显式错误 ----------------

class DouyinDownloadVideoErrorTest(unittest.TestCase):
    """#127 B6：download_video 缺下载地址时显式错误，不再多层裸索引天书。"""

    def test_missing_url_list_raises_clear_error(self):
        from app.downloaders.douyin_downloader import DouyinDownloader

        d = DouyinDownloader()
        out_dir = tempfile.mkdtemp(prefix="vn_dy_")
        try:
            with mock.patch.object(d, "extract_video_id", return_value="123"):
                with mock.patch.object(d, "fetch_video_info", return_value={"aweme_detail": {"aweme_id": "123", "video": {}}}):
                    with mock.patch("app.downloaders.douyin_downloader.get_data_dir", return_value=out_dir):
                        with self.assertRaises(ValueError) as cm:
                            d.download_video("https://v.douyin.com/x")
            # download_video 把内部错误包成「抖音下载请求失败: ...」，原因透传
            self.assertIn("视频下载地址", str(cm.exception))
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_missing_aweme_id_raises_clear_error(self):
        from app.downloaders.douyin_downloader import DouyinDownloader

        d = DouyinDownloader()
        out_dir = tempfile.mkdtemp(prefix="vn_dy_")
        try:
            with mock.patch.object(d, "extract_video_id", return_value="123"):
                with mock.patch.object(d, "fetch_video_info", return_value={"aweme_detail": {}}):
                    with mock.patch("app.downloaders.douyin_downloader.get_data_dir", return_value=out_dir):
                        with self.assertRaises(ValueError) as cm:
                            d.download_video("https://v.douyin.com/x")
            self.assertIn("aweme_id", str(cm.exception))
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)


# ---------------- B7：universal_gpt env 垃圾值回退 ----------------

class UniversalGPTEnvTest(unittest.TestCase):
    """#127 B7：env 数值垃圾值 warning 回退默认，不再裸 int()/float() 崩任务。"""

    def test_bad_env_falls_back(self):
        from app.gpt.universal_gpt import UniversalGPT

        bad = {
            "OPENAI_MAX_TOKENS_PER_CHUNK": "abc",
            "OPENAI_RETRY_ATTEMPTS": "xyz",
            "OPENAI_RETRY_BACKOFF_SECONDS": "oops",
        }
        with mock.patch.dict(os.environ, bad, clear=False):
            g = UniversalGPT(client=mock.Mock(), model="m")
        self.assertEqual(g.max_tokens_per_chunk, 12000)
        self.assertEqual(g._max_retry_attempts, 3)
        self.assertEqual(g._retry_base_backoff, 1.5)


# ---------------- B8：provider.add_provider 走 logger ----------------

class ProviderAddLoggingTest(unittest.TestCase):
    """#127 B8：失败走 logger.error 而非 print（MCP 下 stdout 被劫持）。"""

    def test_failure_uses_logger_not_print(self):
        from app.services import provider as provider_mod

        with mock.patch.object(provider_mod.logger, "error") as m_err:
            with mock.patch.object(provider_mod, "get_provider_by_name", return_value={"name": "dup"}):
                with self.assertRaises(ValueError):
                    provider_mod.ProviderService.add_provider(
                        name="dup", api_key="", base_url="http://x", logo="", type_="custom"
                    )
        self.assertTrue(m_err.called)
        self.assertIn("创建模式失败", m_err.call_args.args[0])


class ProviderBaseUrlUserinfoRejectedTest(unittest.TestCase):
    """#140 复扫 A3：base_url 携带 user:pass@ 会明文落库——add/update 均拒绝。"""

    def test_add_rejects_userinfo(self):
        from app.services.provider import ProviderService

        with self.assertRaises(ValueError) as cm:
            ProviderService.add_provider(
                name="x", api_key="sk", base_url="https://user:secret@relay.example.com/v1",
                logo="", type_="custom",
            )
        self.assertIn("user:pass@", str(cm.exception))
        self.assertNotIn("secret", str(cm.exception))  # 错误消息本身不泄凭据

    def test_update_rejects_userinfo(self):
        from app.services.provider import ProviderService

        with self.assertRaises(ValueError) as cm:
            ProviderService.update_provider("p1", {"base_url": "http://u:p@127.0.0.1:11434/v1"})
        self.assertIn("user:pass@", str(cm.exception))
        self.assertNotIn("u:p", str(cm.exception))

    def test_plain_base_url_still_allowed(self):
        from app.services.provider import ProviderService

        self.assertEqual(
            ProviderService._validate_base_url("https://relay.example.com/v1"),
            "https://relay.example.com/v1",
        )
        self.assertEqual(
            ProviderService._validate_base_url("http://127.0.0.1:11434/v1"),
            "http://127.0.0.1:11434/v1",
        )


# ---------------- B9：local convert_to_mp3 默认数据目录 ----------------

class LocalMp3DefaultDirTest(unittest.TestCase):
    """#127 B9：output_path 缺省落数据目录而非源文件同目录。"""

    def test_default_output_in_data_dir(self):
        from app.downloaders.local_downloader import LocalDownloader

        d = LocalDownloader()
        src_dir = tempfile.mkdtemp(prefix="vn_src_")
        src = os.path.join(src_dir, "clip.mp4")
        data_dir = tempfile.mkdtemp(prefix="vn_data_")
        out = os.path.join(data_dir, "clip.mp3")
        try:
            # convert_to_mp3 内 `from app.utils.path_helper import get_data_dir`——
            # 需 patch path_helper 而非 local_downloader 模块属性
            with mock.patch("app.utils.path_helper.get_data_dir", return_value=data_dir):
                with mock.patch("app.downloaders.local_downloader.subprocess.run"):
                    with mock.patch(
                        "app.downloaders.local_downloader.os.path.exists",
                        side_effect=lambda p: p == src or p == out,
                    ):
                        got = d.convert_to_mp3(src)
            self.assertEqual(got, out)
            # 源目录无产物、数据目录有
            self.assertFalse(os.path.exists(os.path.join(src_dir, "clip.mp3")))
        finally:
            shutil.rmtree(src_dir, ignore_errors=True)
            shutil.rmtree(data_dir, ignore_errors=True)


# ---------------- B10：note._save_metadata title None ----------------

class NoteSaveMetadataNoneTest(unittest.TestCase):
    """#127 B10：title=None 时日志不再 TypeError（被 except 吞后误报「保存失败」）。"""

    def test_title_none_no_typeerror(self):
        from app.services import note as note_mod

        gen = object.__new__(note_mod.NoteGenerator)  # 绕过 __init__，只测 _save_metadata
        with mock.patch("app.services.note.insert_video_task") as m_ins:
            with mock.patch.object(note_mod.logger, "info"):
                gen._save_metadata(
                    video_id="v1", platform="local", task_id="t1",
                    title=None, status="SUCCESS", note_dir="d",
                )
        m_ins.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
