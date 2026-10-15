"""Explicit setup update checks and trusted project links; no startup I/O."""
import json
import webbrowser
from dataclasses import dataclass
from http.client import HTTPException
from urllib.error import URLError
from urllib.request import Request, urlopen

from packaging.version import Version

from videonote_mcp import __version__
from videonote_mcp.guidance import project_info

PYPI_URL = "https://pypi.org/pypi/videonote/json"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
CHECK_TIMEOUT = 5
_project = project_info(__version__)
PROJECT_LINKS = {
    "repository": _project["repository"],
    "releases": _project["documentation"]["releases"],
    "manual": _project["documentation"]["manual"],
}


class UpdateCheckError(Exception):
    """A safe, user-facing failure without network/proxy details."""


@dataclass(frozen=True)
class UpdateResult:
    latest: str
    # Positive means a newer published version is available.
    comparison: int


def check_update(current: str = __version__) -> UpdateResult:
    """Query PyPI only on request, with bounded reads and PEP 440 comparison."""
    request = Request(PYPI_URL, headers={
        "Accept": "application/json", "User-Agent": f"VideoNote/{__version__}",
    })
    try:
        with urlopen(request, timeout=CHECK_TIMEOUT) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError("oversized response")
        data = json.loads(payload)
        latest = data["info"]["version"]
        if not isinstance(latest, str) or len(latest) > 128:
            raise ValueError("invalid version")
        published, installed = Version(latest), Version(current)
        return UpdateResult(str(published), (published > installed) - (published < installed))
    except (OSError, URLError, HTTPException):
        raise UpdateCheckError("检查失败：无法连接 PyPI，请检查网络后重试，或打开发行说明。") from None
    except (ValueError, KeyError, TypeError, RecursionError):
        raise UpdateCheckError("检查失败：PyPI 返回的版本信息无效，请稍后重试，或打开发行说明。") from None


def open_project_link(action: str) -> bool:
    """Only open known project pages. Callers display the URL on failure."""
    url = PROJECT_LINKS[action]
    try:
        return bool(webbrowser.open(url, new=2))
    except (webbrowser.Error, OSError):
        return False
