"""LaunchAgent definition for the proxy service (no launchctl calls)."""

import sys

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
