"""Setup version/update actions never touch real network, browsers or configs."""
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from videonote_mcp import __version__, setup_about


@pytest.mark.parametrize(("current", "latest", "comparison"), [
    ("0.2.9", "0.2.10", 1), ("0.2.7", "0.2.7", 0),
    ("0.2.8", "0.2.7", -1), ("0.2.7rc1", "0.2.7", 1),
    ("0.2.7+local", "0.2.7", -1),
])
def test_check_update_compares_published_versions(monkeypatch, current, latest, comparison):
    def fetch(request, timeout):
        assert request.full_url == "https://pypi.org/pypi/videonote/json"
        assert timeout == 5
        return io.BytesIO(json.dumps({"info": {"version": latest}}).encode())

    monkeypatch.setattr(setup_about, "urlopen", fetch)
    assert setup_about.check_update(current) == setup_about.UpdateResult(latest, comparison)


@pytest.mark.parametrize("payload", [
    b"no json", b"[]", b"{}", b'{"info":null}', b'{"info":{"version":3}}',
    b'{"info":{"version":"bad version"}}',
    b'{"info":{"version":"' + b"1" * 129 + b'"}}',
    b"x" * (setup_about.MAX_RESPONSE_BYTES + 1),
])
def test_invalid_response_is_actionable(monkeypatch, payload):
    class BoundedResponse(io.BytesIO):
        def read(self, size=-1):
            assert size == setup_about.MAX_RESPONSE_BYTES + 1
            return super().read(size)

    monkeypatch.setattr(setup_about, "urlopen", lambda *a, **k: BoundedResponse(payload))
    with pytest.raises(setup_about.UpdateCheckError, match="版本信息无效"):
        setup_about.check_update()


@pytest.mark.parametrize("error", [
    TimeoutError("proxy password"), URLError("proxy password"),
    HTTPError("https://user:password@proxy", 503, "proxy password", {}, None),
])
def test_network_errors_are_safe_and_retryable(monkeypatch, error):
    def fetch(*args, **kwargs):
        raise error

    monkeypatch.setattr(setup_about, "urlopen", fetch)
    with pytest.raises(setup_about.UpdateCheckError, match="无法连接 PyPI") as caught:
        setup_about.check_update()
    assert "password" not in str(caught.value)


@pytest.mark.parametrize("action", ["repository", "releases", "manual"])
def test_project_links_use_known_urls(monkeypatch, action):
    calls = []
    monkeypatch.setattr(setup_about.webbrowser, "open", lambda url, new: calls.append((url, new)) or True)
    assert setup_about.open_project_link(action)
    assert calls == [(setup_about.PROJECT_LINKS[action], 2)]


@pytest.mark.parametrize("error", [None, OSError("no browser"), setup_about.webbrowser.Error("no browser")])
def test_browser_failure_can_fall_back_to_url(monkeypatch, error):
    def launch(*args, **kwargs):
        if error:
            raise error
        return False

    monkeypatch.setattr(setup_about.webbrowser, "open", launch)
    assert not setup_about.open_project_link("repository")


class Menu:
    def __init__(self, *answers):
        self.answers = iter(answers)

    def select(self, **kwargs):
        answer = next(self.answers)
        assert answer in [choice["value"] for choice in kwargs["choices"]]
        return SimpleNamespace(execute=lambda: answer)


def output(capsys):
    captured = capsys.readouterr()
    return captured.out + captured.err


def test_main_dispatch_version_and_no_automatic_network(monkeypatch, capsys):
    from videonote_mcp import cli

    monkeypatch.setattr(setup_about, "urlopen", lambda *a, **k: pytest.fail("unexpected network"))
    monkeypatch.setattr(setup_about.webbrowser, "open", lambda *a, **k: pytest.fail("unexpected browser"))
    cli._wizard(Menu("about", "back", "exit"))
    text = output(capsys)
    assert f"配置向导 v{__version__}" in text
    assert "当前 setup 版本" in text
    assert "尚未检查" in text


@pytest.mark.parametrize(("comparison", "expected"), [(1, "发现新版本"), (0, "已是 PyPI 最新"), (-1, "高于 PyPI 最新")])
def test_about_check_displays_result(monkeypatch, capsys, comparison, expected):
    from videonote_mcp import cli, installer

    monkeypatch.setattr(installer, "source_root", lambda: None)
    monkeypatch.setattr(setup_about, "check_update", lambda: setup_about.UpdateResult("0.2.8", comparison))
    cli._wizard_about(Menu("check", "back"))
    text = output(capsys)
    assert expected in text
    assert "uvx videonote@latest install" in text


def test_about_failure_then_retry_and_browser_fallback(monkeypatch, capsys):
    from videonote_mcp import cli

    answers = iter([setup_about.UpdateCheckError("检查失败：请重试"), setup_about.UpdateResult(__version__, 0)])

    def check():
        result = next(answers)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(setup_about, "check_update", check)
    monkeypatch.setattr(setup_about, "open_project_link", lambda action: False)
    cli._wizard_about(Menu("check", "check", "repository", "back"))
    text = output(capsys)
    assert "检查失败：请重试" in text
    assert "已是 PyPI 最新" in text
    assert "请手动访问：" + setup_about.PROJECT_LINKS["repository"] in text


def test_about_numeric_fallback(monkeypatch, capsys):
    from videonote_mcp import cli

    answers = iter(["4", "0"])
    opened = []
    monkeypatch.setattr(cli, "_ask", lambda *a, **k: next(answers))
    monkeypatch.setattr(setup_about, "open_project_link", lambda action: opened.append(action) or True)
    cli._wizard_about()
    assert opened == ["manual"]
    assert "已请求浏览器打开" in output(capsys)


def test_about_interrupt_returns_to_parent():
    from videonote_mcp import cli

    class Interrupted:
        def select(self, **kwargs):
            raise KeyboardInterrupt

    cli._wizard_about(Interrupted())
