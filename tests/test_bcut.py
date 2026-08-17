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


if __name__ == "__main__":
    unittest.main()
