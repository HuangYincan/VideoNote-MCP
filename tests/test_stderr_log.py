"""stderr 日志轮转 / 打开失败可见 / 退出摘要（docs/05 #44 可观测性收口）。

不写真实数据目录：mock server.DATA_DIR 到临时目录。
"""

from unittest import mock

from videonote_mcp import server


class TestOpenStderrLog:
    def test_opens_log_in_data_dir(self, tmp_path):
        with mock.patch.object(server, "DATA_DIR", tmp_path):
            f = server._open_stderr_log()
            assert f is not None
            assert (tmp_path / "logs" / "mcp_stderr.log").exists()
            f.close()

    def test_rotates_when_over_limit(self, tmp_path):
        with mock.patch.object(server, "DATA_DIR", tmp_path):
            log = tmp_path / "logs" / "mcp_stderr.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_bytes(b"x" * (50 * 1024 * 1024 + 1))
            f = server._open_stderr_log()
            assert f is not None
            assert (tmp_path / "logs" / "mcp_stderr.log.1").exists()
            assert log.stat().st_size == 0
            f.close()

    def test_no_rotation_under_limit(self, tmp_path):
        with mock.patch.object(server, "DATA_DIR", tmp_path):
            log = tmp_path / "logs" / "mcp_stderr.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_bytes(b"small")
            f = server._open_stderr_log()
            assert f is not None
            assert not (tmp_path / "logs" / "mcp_stderr.log.1").exists()
            f.close()

    def test_env_max_mb(self, tmp_path):
        with mock.patch.object(server, "DATA_DIR", tmp_path), \
             mock.patch.dict("os.environ", {"VIDEONOTE_STDERR_LOG_MAX_MB": "1"}, clear=False):
            log = tmp_path / "logs" / "mcp_stderr.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_bytes(b"x" * (1024 * 1024 + 1))
            f = server._open_stderr_log()
            assert (tmp_path / "logs" / "mcp_stderr.log.1").exists()
            f.close()

    def test_failure_returns_none_and_prints_reason(self, tmp_path, capsys):
        with mock.patch.object(server, "DATA_DIR", tmp_path), \
             mock.patch("builtins.open", side_effect=OSError("disk full")), \
             mock.patch.object(server.sys, "stderr", mock.Mock()) as fake_err:
            assert server._open_stderr_log() is None
            assert fake_err.write.called
            assert "stderr 日志失败" in "".join(
                c.args[0] for c in fake_err.write.call_args_list)


class TestExitSummary:
    def test_logs_active_task_count(self):
        with mock.patch.object(server, "logger") as fake_logger, \
             mock.patch.object(server, "_task_futures", {"a": object(), "b": object()}):
            server._exit_summary()
            fake_logger.info.assert_called_once()
            assert "2" in fake_logger.info.call_args[0][0]

    def test_never_raises(self):
        with mock.patch.object(server, "logger", side_effect=Exception("boom")):
            server._exit_summary()  # 不抛异常即通过
