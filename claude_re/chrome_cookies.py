"""Extract claude.ai cookies from a locally installed Chrome profile.

Drop-in alternative to --auto-fetch that reuses an *already logged-in*
browser instead of launching a fresh profile: it reads the profile's cookie
database and decrypts it with the browser's OS-level key.

Supported:
  - macOS Chrome / Chromium / Brave / Edge   (v10, AES-128-CBC, Keychain key)
  - Linux Chrome / Chromium (v10, "peanuts" key)
Not supported:
  - Windows (cookie values are DPAPI-protected) — use --auto-fetch there.

Decryption uses the system `openssl` binary for AES-128-CBC. No cookies leave
the machine; only the claude.ai rows are read.
"""
import glob
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

# Browser family → (profile root, Keychain service, Keychain account, PBKDF2 iters)
_MAC_BROWSERS = {
    "Chrome":   ("~/Library/Application Support/Google/Chrome",
                 "Chrome Safe Storage", "Chrome", 1003),
    "Chromium": ("~/Library/Application Support/Chromium",
                 "Chromium Safe Storage", "Chromium", 1003),
    "Brave":    ("~/Library/Application Support/BraveSoftware/Brave-Browser",
                 "Brave Safe Storage", "Brave", 1003),
    "Edge":     ("~/Library/Application Support/Microsoft Edge",
                 "Microsoft Edge Safe Storage", "Microsoft Edge", 1003),
}
_LINUX_BROWSERS = {
    "Chrome":   ("~/.config/google-chrome", None, None, 1),
    "Chromium": ("~/.config/chromium", None, None, 1),
    "Brave":    ("~/.config/BraveSoftware/Brave-Browser", None, None, 1),
}


class CookieExtractionError(RuntimeError):
    """Raised when no usable claude.ai session can be read from a profile."""


def _browser_table():
    return _MAC_BROWSERS if sys.platform == "darwin" else _LINUX_BROWSERS


def _profile_dirs(root):
    """Return [Default, Profile *] dirs under a browser root that hold cookies."""
    dirs = []
    for name in ["Default"] + sorted(
            os.path.basename(p) for p in glob.glob(os.path.join(root, "Profile *"))):
        d = os.path.join(root, name)
        if os.path.exists(os.path.join(d, "Cookies")):
            dirs.append(d)
    return dirs


def _info_for(profile_dir, browser):
    _root, service, account, iters = _browser_table()[browser]
    return {"browser": browser, "dir": profile_dir, "service": service,
            "account": account, "iterations": iters}


def _browser_for_profile(profile_dir):
    """Identify which browser a resolved profile dir belongs to."""
    p = os.path.abspath(profile_dir)
    for name, (root, _s, _a, _i) in _browser_table().items():
        if os.path.abspath(os.path.expanduser(root)) in p:
            return name
    return "Chrome"


def list_profiles(explicit=None):
    """Yield profile descriptors ({dir, browser, service, account, iterations}).

    explicit: a profile dir, a browser root, or a path to a Cookies file.
    """
    if explicit:
        p = os.path.abspath(os.path.expanduser(explicit))
        if os.path.basename(p) == "Cookies":
            p = os.path.dirname(p)
        browser = _browser_for_profile(p)
        if os.path.exists(os.path.join(p, "Cookies")):
            return [_info_for(p, browser)]
        dirs = _profile_dirs(p)
        if dirs:
            return [_info_for(d, browser) for d in dirs]
        raise CookieExtractionError(f"no Chrome Cookies database under {explicit}")

    out = []
    for browser, (root, _s, _a, _i) in _browser_table().items():
        root = os.path.expanduser(root)
        if os.path.isdir(root):
            out.extend(_info_for(d, browser) for d in _profile_dirs(root))
    if not out:
        raise CookieExtractionError(
            "no Chrome/Chromium profile found — pass --from-chrome PROFILE")
    return out


def _password(info):
    if sys.platform == "darwin":
        print("[*] Requesting the browser Keychain key "
              "(click Allow/Always Allow if macOS prompts)...")
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-w",
                 "-s", info["service"], "-a", info["account"]],
                capture_output=True, text=True, timeout=120)
        except FileNotFoundError:
            raise CookieExtractionError("macOS 'security' tool not found")
        except subprocess.TimeoutExpired:
            raise CookieExtractionError(
                "Keychain access timed out — approve the macOS prompt "
                "(Always Allow) and retry")
        if out.returncode != 0 or not out.stdout.strip():
            raise CookieExtractionError(
                f"could not read '{info['service']}' from the Keychain "
                "(access denied or item missing)")
        return out.stdout.rstrip("\n").encode()
    return b"peanuts"  # Linux v10


def _aes_decrypt(ciphertext, key, iv=b" " * 16):
    try:
        p = subprocess.run(
            ["openssl", "enc", "-aes-128-cbc", "-d", "-nopad",
             "-K", key.hex(), "-iv", iv.hex()],
            input=ciphertext, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise CookieExtractionError(
            "system 'openssl' not found — required to decrypt Chrome cookies")
    if p.returncode != 0 or not p.stdout:
        raise CookieExtractionError(
            "openssl decryption failed (unexpected cookie format or wrong key)")
    return p.stdout


def _decrypt(enc, key):
    """Decrypt one Chrome cookie value (v10/v11 AES-128-CBC)."""
    if isinstance(enc, str):
        enc = enc.encode("utf-8", "surrogateescape")
    if enc[:3] not in (b"v10", b"v11"):
        return enc.decode("utf-8", "replace")  # legacy plaintext entry
    pt = _aes_decrypt(enc[3:], key)
    pad = pt[-1]
    if 1 <= pad <= 16:  # strip PKCS7 padding
        pt = pt[:-pad]
    # Chrome >= 130 prepends a 32-byte SHA256(domain) hash.
    if len(pt) > 32:
        try:
            pt.decode("utf-8")
        except UnicodeDecodeError:
            pt = pt[32:]
    return pt.decode("utf-8", "replace")


def _read_cookie_rows(db_path):
    """Copy the DB (plus WAL/SHM) so a running Chrome doesn't lock us out."""
    tmp = tempfile.mkdtemp(prefix="claude_re_ck_")
    try:
        for suffix in ("", "-wal", "-shm"):
            src = db_path + suffix
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(tmp, "Cookies" + suffix))
        con = sqlite3.connect(os.path.join(tmp, "Cookies"))
        try:
            return con.execute(
                "SELECT name, host_key, encrypted_value, value FROM cookies "
                "WHERE host_key LIKE '%claude.ai%'").fetchall()
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def read_claude_cookies(explicit=None):
    """Return {cookie_name: value} for claude.ai from a logged-in profile.

    Tries each profile in order and returns the first that holds a sessionKey.
    Raises CookieExtractionError with a reason if none work.
    """
    errors = []
    for info in list_profiles(explicit):
        db = os.path.join(info["dir"], "Cookies")
        try:
            rows = _read_cookie_rows(db)
        except Exception as e:  # unreadable/corrupt profile — keep trying
            errors.append(f"{db}: {e}")
            continue
        if not rows:
            continue
        try:
            key = hashlib.pbkdf2_hmac("sha1", _password(info), b"saltysalt",
                                      info["iterations"], 16)
        except CookieExtractionError as e:
            errors.append(f"{info['dir']}: {e}")
            continue

        cookies = {}
        for name, _host, enc, val in rows:
            try:
                cookies[name] = _decrypt(enc, key) if enc else val
            except CookieExtractionError as e:
                errors.append(f"{info['dir']}: {name}: {e}")
        if cookies.get("sessionKey"):
            print(f"[+] Session found: {info['browser']} — {info['dir']}")
            return cookies

    if errors:
        raise CookieExtractionError(
            "could not read a claude.ai session:\n  " + "\n  ".join(errors))
    raise CookieExtractionError(
        "no claude.ai sessionKey found — log in to claude.ai in Chrome first")
