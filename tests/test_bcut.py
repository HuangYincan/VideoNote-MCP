"""必剪（bcut）转写器的上传 code 检查（#121 B8）。

_upload 曾在接口返回业务错误（{code: 非0, message}）时直接取 resp["data"]
→ KeyError 裸崩，调用方只看到天书 traceback。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.transcriber.bcut import BcutTranscriber


def _fake_resp(payload):
    r = mock.Mock()
    r.raise_for_status.return_value = None
    r.json.return_value = payload
    return r


class BcutUploadCodeCheckTest(unittest.TestCase):
    def setUp(self):
        self.tr = BcutTranscriber()
        self.f = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        self.f.write(b"fake-audio-bytes")
        self.f.close()

    def tearDown(self):
        import os

        os.unlink(self.f.name)

    def test_code_nonzero_raises_readable_error(self):
        with mock.patch.object(
            self.tr.session, "post", return_value=_fake_resp({"code": 1, "message": "资源非法"})
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.tr._upload(self.f.name)
        msg = str(ctx.exception)
        self.assertIn("必剪申请上传失败", msg)
        self.assertIn("资源非法", msg)  # 业务原因可见，而非 KeyError

    def test_ok_path_still_works(self):
        payload = {
            "code": 0,
            "data": {
                "in_boss_key": "key1",
                "resource_id": "res1",
                "upload_id": "up1",
                "upload_urls": ["http://up/1"],
                "per_size": 1024,
                "size": 2048,
            },
        }
        with mock.patch.object(self.tr.session, "post", return_value=_fake_resp(payload)):
            with mock.patch.object(self.tr, "_BcutTranscriber__upload_part") as m_part, mock.patch.object(
                self.tr, "_BcutTranscriber__commit_upload"
            ) as m_commit:
                self.tr._upload(self.f.name)
        m_part.assert_called_once()
        m_commit.assert_called_once()
        self.assertEqual(self.tr._BcutTranscriber__upload_id, "up1")


class BcutSessionLifecycleTest(unittest.TestCase):
    """requests.Session 连接池/打开 fd 需释放（#123 B11）：close() 显式关闭，__del__ 兜底。"""

    def test_close_releases_session(self):
        tr = BcutTranscriber()
        self.assertIsNotNone(tr.session)
        with mock.patch.object(tr.session, "close") as m_close:
            tr.close()
        m_close.assert_called_once()
        self.assertIsNone(tr.session)  # 置 None：后续 __del__ 不再重复 close

    def test_del_closes_session(self):
        tr = BcutTranscriber()
        with mock.patch.object(tr.session, "close") as m_close:
            tr.__del__()
        m_close.assert_called_once()

    def test_close_idempotent_and_del_safe(self):
        tr = BcutTranscriber()
        tr.close()
        tr.close()  # 幂等
        tr.__del__()  # session 已 None → 不抛


class BcutEtagHeaderNoneTest(unittest.TestCase):
    """分片上传响应 header 存在但值为 None 时不再 AttributeError（#124 B10）。"""

    def test_etag_none_header_safe(self):
        tr = BcutTranscriber()
        # 手动铺分片状态，绕过申请上传接口
        tr._BcutTranscriber__clips = 1
        tr._BcutTranscriber__per_size = 1024
        tr._BcutTranscriber__upload_urls = ["http://up/1"]
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.headers = {"Etag": None}  # 异常网关响应：header 在但值为 None
        with mock.patch.object(tr.session, "put", return_value=resp):
            tr._BcutTranscriber__upload_part(b"x" * 2048)
        self.assertEqual(tr._BcutTranscriber__etags, [""])  # None → ""，不崩

    def test_etag_present_keeps_value(self):
        tr = BcutTranscriber()
        tr._BcutTranscriber__clips = 1
        tr._BcutTranscriber__per_size = 1024
        tr._BcutTranscriber__upload_urls = ["http://up/1"]
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.headers = {"Etag": "\"abc123\""}  # 带引号正常值 → 去引号
        with mock.patch.object(tr.session, "put", return_value=resp):
            tr._BcutTranscriber__upload_part(b"x" * 2048)
        self.assertEqual(tr._BcutTranscriber__etags, ["abc123"])


if __name__ == "__main__":
    unittest.main()
