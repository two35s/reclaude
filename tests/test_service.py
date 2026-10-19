"""LaunchAgent definition for the proxy service (no launchctl calls)."""

import io
import sys

from si import service
from si.config import DIR
from si.service import LABEL, _plist


def test_plist_runs_the_proxy_with_this_interpreter():
    plist = _plist(8765)
    assert plist["Label"] == LABEL
    assert plist["ProgramArguments"] == [
        sys.executable,
        "-u",
        "-m",
        "si",
        "--proxy",
        "--port",
        "8765",
    ]
    assert plist["WorkingDirectory"] == DIR
    assert plist["RunAtLoad"] and plist["KeepAlive"]
    assert plist["StandardOutPath"].endswith("si-proxy.log")


def test_plist_carries_the_proxy_token_when_set(monkeypatch):
    monkeypatch.setenv("SI_PROXY_TOKEN", "secret")
    assert _plist(8765)["EnvironmentVariables"] == {"SI_PROXY_TOKEN": "secret"}


def test_health_check_sends_the_proxy_token(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["auth"] = req.headers.get("Authorization")
        return io.BytesIO(b"{}")

    monkeypatch.setenv("SI_PROXY_TOKEN", "secret")
    monkeypatch.setattr(service.urllib.request, "urlopen", fake_urlopen)
    assert service._healthy(8765) is True
    assert seen["auth"] == "Bearer secret"
