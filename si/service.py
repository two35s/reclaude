"""macOS LaunchAgent for the proxy.

Clients (OpenCode, Crush) only see the models while the proxy is running; when
it isn't, every request fails with ConnectionRefused.  This installs it as a
user LaunchAgent so it starts at login and restarts if it exits.
"""

import os
import plistlib
import socket
import subprocess
import sys
import time
import urllib.request

from .config import DIR

LABEL = "com.si.proxy"
PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
LOG_PATH = os.path.expanduser("~/Library/Logs/si-proxy.log")


def _plist(port):
    """The LaunchAgent definition (pure, so tests never touch launchctl)."""
    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-u", "-m", "si", "--proxy", "--port", str(port)],
        "WorkingDirectory": DIR,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": LOG_PATH,
        "StandardErrorPath": LOG_PATH,
    }
    if os.environ.get("SI_PROXY_TOKEN"):
        plist["EnvironmentVariables"] = {"SI_PROXY_TOKEN": os.environ["SI_PROXY_TOKEN"]}
    return plist


def _launchctl(*argv):
    return subprocess.run(["launchctl", *argv], capture_output=True, text=True)


def _healthy(port):
    req = urllib.request.Request(f"http://localhost:{port}/v1/models")
    token = os.environ.get("SI_PROXY_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=1):
            return True
    except OSError:
        return False


def _port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def install(port=8765):
    if sys.platform != "darwin":
        print("[!] --install-service is macOS-only; on Linux use a systemd user unit.")
        return False
    # Replacing an existing agent: unload ours first so the port is free and
    # the check below only sees foreign processes.  A TCP check catches any
    # listener, not only a healthy /v1/models, which would otherwise make
    # launchd crash-loop on bind.
    if os.path.exists(PLIST_PATH):
        _launchctl("unload", "-w", PLIST_PATH)
    for _ in range(10):
        if not _port_in_use(port):
            break
        time.sleep(0.5)
    else:
        print(f"[!] port {port} is already in use — stop that process first")
        return False

    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(_plist(port), f)
    if os.name != "nt":
        os.chmod(PLIST_PATH, 0o600)  # the plist may embed SI_PROXY_TOKEN
    result = _launchctl("load", "-w", PLIST_PATH)
    if result.returncode != 0:
        print(f"[!] launchctl load failed: {(result.stderr or result.stdout).strip()}")
        return False

    for _ in range(10):
        if _healthy(port):
            print(f"[+] proxy service installed and answering on http://localhost:{port}/v1")
            print("    starts at login, restarts if it exits")
            print(f"    plist: {PLIST_PATH}")
            print(f"    log:   {LOG_PATH}")
            return True
        time.sleep(0.5)
    print(f"[!] service loaded but http://localhost:{port}/v1 did not answer — see {LOG_PATH}")
    return False


def uninstall():
    if sys.platform != "darwin":
        print("[!] --uninstall-service is macOS-only")
        return False
    if not os.path.exists(PLIST_PATH):
        print("[*] no proxy service installed")
        return True
    _launchctl("unload", "-w", PLIST_PATH)
    os.remove(PLIST_PATH)
    print("[+] proxy service removed")
    return True
