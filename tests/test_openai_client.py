"""build_openai_client 超时收敛（#113）：默认连接 10s / 读写 300s，显式传参覆盖。

此前 timeout=None → openai SDK 默认 600s（代理路径另写 600s）——上游挂死时
LLM 调用卡 10 分钟 ×重试次数，占死 worker 槽。默认超时在构造点单点收敛，
LLM 调用（gpt_factory）与 groq 转写都吃这个默认。
"""
import os
import unittest
from unittest import mock

from app.utils.openai_client import build_openai_client


class BuildOpenaiClientTimeoutTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(
            os.environ,
            {"VIDEONOTE_DATA_DIR": "/tmp/vn_oc_test_data", "VIDEONOTE_CONFIG_DIR": "/tmp/vn_oc_test_cfg"},
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_default_timeout_bounded(self):
        # 缺省 → 连接 10s 快速失败 + 读写 300s 兜底死读（不再吃 SDK 600s 默认）
        client = build_openai_client("test-key", "https://api.example.com")
        self.assertEqual(client.timeout.connect, 10.0)
        self.assertEqual(client.timeout.read, 300.0)

    def test_explicit_timeout_override(self):
        # 显式传参覆盖（连通性测试 15s 等），不被默认值抢走（float 原样交给 SDK）
        client = build_openai_client("test-key", "https://api.example.com", timeout=15.0)
        self.assertEqual(client.timeout, 15.0)

    def test_proxy_path_uses_same_timeout(self):
        # 代理路径的 httpx.Client 与客户端超时一致（此前代理分支另写 600s）
        with mock.patch(
            "app.utils.openai_client.ProxyConfigManager.get_proxy_url",
            return_value="http://127.0.0.1:9999",
        ):
            client = build_openai_client("test-key", "https://api.example.com")
        self.assertEqual(client.timeout.connect, 10.0)
        self.assertEqual(client.timeout.read, 300.0)
        # http_client 已注入（proxy 生效，docs/05 #74 的释放逻辑依赖此路径）
        self.assertIsNotNone(client._client)  # 私有字段仅用于验证注入

    def test_empty_key_still_rejected(self):
        with self.assertRaises(ValueError):
            build_openai_client("", "https://api.example.com")


if __name__ == "__main__":
    unittest.main()
