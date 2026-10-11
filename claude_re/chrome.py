"""Real-Chrome helpers (credential extraction / login only)."""
import os
import shutil
import socket
import subprocess
import time

from .config import PROFILE


def find_chrome():
    """Find Chrome executable (Windows, macOS, Linux)."""
    candidates = [
        # Windows
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        # macOS (.app bundles aren't on PATH)
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        # Linux
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/opt/google/chrome/chrome",
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            print(f"[+] Found Chrome: {c}")
            return c
    return None


def get_free_port():
    """Get a random free port from the OS."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def launch_real_chrome(chrome_path, url="https://claude.ai"):
    """Launch a real Chrome process with remote debugging.

    Security: bound to 127.0.0.1 only, random ephemeral port.
    This is a normal Chrome — NOT controlled by Playwright/CDP at launch.
    Cloudflare cannot distinguish it from a regular user session.
    """
    os.makedirs(PROFILE, exist_ok=True)
    cdp_port = get_free_port()
    cmd = [
        chrome_path,
        f"--remote-debugging-port={cdp_port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        url
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[+] Real Chrome launched (PID {proc.pid}, CDP 127.0.0.1:{cdp_port})")
        time.sleep(3)
        return proc, cdp_port
    except Exception as e:
        print(f"[!] Failed to launch Chrome: {e}")
        return None, 0


def do_login():
    """Launch real Chrome for the user to log in. Session is saved to profile dir."""
    chrome = find_chrome()
    if not chrome:
        print("[!] Chrome not found. Install Google Chrome or use --manual.")
        return

    print("=" * 55)
    print("  Real Chrome will open → log in to Claude.ai")
    print("  CF will pass automatically (real browser)")
    print("  Close Chrome when done.")
    print("=" * 55)
    input("  Press ENTER...")

    proc, _ = launch_real_chrome(chrome)
    if not proc:
        return

    print("[*] Waiting for Chrome to close...")
    proc.wait()
    print("[+] Profile saved to claude_profile/\n")
