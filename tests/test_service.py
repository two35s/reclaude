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


def test_install_writes_a_private_plist(tmp_path, monkeypatch):
    import subprocess

    plist = tmp_path / "com.si.proxy.plist"
    monkeypatch.setattr(service, "PLIST_PATH", str(plist))
    monkeypatch.setattr(service, "LOG_PATH", str(tmp_path / "si-proxy.log"))
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service, "_port_in_use", lambda port: False)
    monkeypatch.setattr(service, "_healthy", lambda port: True)
    monkeypatch.setattr(service, "_launchctl", lambda *a: subprocess.CompletedProcess(a, 0, "", ""))

    assert service.install(8765) is True
    assert (plist.stat().st_mode & 0o777) == 0o600
