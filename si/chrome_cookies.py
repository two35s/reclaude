"""Extract site cookies (claude.ai / chatgpt.com) from a local Chrome profile.

Drop-in alternative to --auto-fetch that reuses an *already logged-in*
browser instead of launching a fresh profile: it reads the profile's cookie
database and decrypts it with the browser's OS-level key.

Supported:
  - macOS Chrome / Chromium / Brave / Edge   (v10, AES-128-CBC, Keychain key)
  - Linux Chrome / Chromium (v10, "peanuts" key)
Not supported:
  - Windows (cookie values are DPAPI-protected) — use --auto-fetch there.

Decryption uses the system `openssl` binary for AES-128-CBC. No cookies leave
the machine; only the requested site's rows are read.
"""

import glob
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

from .config import (
    AES_BLOCK_SIZE,
    AES_IV_SIZE,
    CHROME_COOKIE_SCHEMA_VERSION_V24,
    COOKIE_SITES,
    PBKDF2_ITERATIONS_LINUX,
    PBKDF2_ITERATIONS_MAC,
)

# Browser family → (profile root, Keychain service, Keychain account, PBKDF2 iters)
_MAC_BROWSERS = {
    "Chrome": (
        "~/Library/Application Support/Google/Chrome",
        "Chrome Safe Storage",
        "Chrome",
        PBKDF2_ITERATIONS_MAC,
    ),
    "Chromium": (
        "~/Library/Application Support/Chromium",
        "Chromium Safe Storage",
        "Chromium",
        PBKDF2_ITERATIONS_MAC,
    ),
    "Brave": (
        "~/Library/Application Support/BraveSoftware/Brave-Browser",
        "Brave Safe Storage",
        "Brave",
        PBKDF2_ITERATIONS_MAC,
    ),
    "Edge": (
        "~/Library/Application Support/Microsoft Edge",
        "Microsoft Edge Safe Storage",
        "Microsoft Edge",
        PBKDF2_ITERATIONS_MAC,
    ),
}
_LINUX_BROWSERS = {
    "Chrome": ("~/.config/google-chrome", None, None, PBKDF2_ITERATIONS_LINUX),
    "Chromium": ("~/.config/chromium", None, None, PBKDF2_ITERATIONS_LINUX),
    "Brave": ("~/.config/BraveSoftware/Brave-Browser", None, None, PBKDF2_ITERATIONS_LINUX),
}


class CookieExtractionError(RuntimeError):
    """Raised when no usable claude.ai session can be read from a profile."""


_OPENSSL_CANDIDATES = (
    "/opt/homebrew/opt/openssl/bin/openssl",
    "/usr/local/opt/openssl/bin/openssl",
    "/usr/bin/openssl",
)


def _tool_path(name, *absolute_candidates):
    """Resolve a helper binary, preferring absolute paths over PATH lookup."""
    for p in absolute_candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    found = shutil.which(name)
    if found:
        return found
    raise CookieExtractionError(f"system '{name}' not found — required for cookie extraction")


def _browser_table():
    return _MAC_BROWSERS if sys.platform == "darwin" else _LINUX_BROWSERS


def _profile_dirs(root):
    """Return [Default, Profile *] dirs under a browser root that hold cookies."""
    dirs = []
    for name in ["Default"] + sorted(
        os.path.basename(p) for p in glob.glob(os.path.join(root, "Profile *"))
    ):
        d = os.path.join(root, name)
        if os.path.exists(os.path.join(d, "Cookies")):
            dirs.append(d)
    return dirs


def _info_for(profile_dir, browser):
    _root, service, account, iters = _browser_table()[browser]
    return {
        "browser": browser,
        "dir": profile_dir,
        "service": service,
        "account": account,
        "iterations": iters,
    }


def _browser_for_profile(profile_dir):
    """Identify which browser a resolved profile dir belongs to."""
    p = os.path.abspath(profile_dir)
    for name, (root, _s, _a, _i) in _browser_table().items():
        root = os.path.abspath(os.path.expanduser(root))
        if p == root or p.startswith(root + os.sep):
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
        raise CookieExtractionError("no Chrome/Chromium profile found — pass --from-chrome PROFILE")
    return out


def _password(info):
    if sys.platform == "darwin":
        print(
            "[*] Requesting the browser Keychain key (click Allow/Always Allow if macOS prompts)..."
        )
        try:
            out = subprocess.run(
                [
                    _tool_path("security", "/usr/bin/security"),
                    "find-generic-password",
                    "-w",
                    "-s",
                    info["service"],
                    "-a",
                    info["account"],
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            raise CookieExtractionError("macOS 'security' tool not found")
        except subprocess.TimeoutExpired:
            raise CookieExtractionError(
                "Keychain access timed out — approve the macOS prompt (Always Allow) and retry"
            )
        if out.returncode != 0 or not out.stdout.strip():
            raise CookieExtractionError(
                f"could not read '{info['service']}' from the Keychain "
                "(access denied or item missing)"
            )
        return out.stdout.rstrip("\n").encode()
    return b"peanuts"  # Linux v10


def _openssl_argv(key, iv):
    return [
        _tool_path("openssl", *_OPENSSL_CANDIDATES),
        "enc",
        "-aes-128-cbc",
        "-d",
        "-nopad",
        "-K",
        key.hex(),
        "-iv",
        iv.hex(),
    ]


def _aes_decrypt(ciphertext, key, iv=b" " * AES_IV_SIZE):
    try:
        p = subprocess.run(
            _openssl_argv(key, iv),
            input=ciphertext,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        raise CookieExtractionError(
            "system 'openssl' not found — required to decrypt Chrome cookies"
        )
    if p.returncode != 0 or not p.stdout:
        raise CookieExtractionError(
            "openssl decryption failed (unexpected cookie format or wrong key)"
        )
    return p.stdout


def _aes_decrypt_many(ciphertexts, key, iv=b" " * AES_IV_SIZE):
    """Decrypt several ciphertexts in one openssl call (one process, not N).

    Every v10/v11 value is AES-block-aligned, so their concatenation
    decrypts as one stream and we split the output by input length.
    Returns None when openssl rejects the batch, so a single malformed
    value can be retried individually instead of losing the whole profile.
    """
    joined = b"".join(ciphertexts)
    try:
        p = subprocess.run(
            _openssl_argv(key, iv),
            input=joined,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        raise CookieExtractionError(
            "system 'openssl' not found — required to decrypt Chrome cookies"
        )
    if p.returncode != 0 or len(p.stdout) != len(joined):
        return None
    out, pos = [], 0
    for ct in ciphertexts:
        out.append(p.stdout[pos : pos + len(ct)])
        pos += len(ct)
    return out


def _decode_cookie(pt, strip_prefix):
    pad = pt[-1]
    if 1 <= pad <= AES_BLOCK_SIZE:  # strip PKCS7 padding
        pt = pt[:-pad]
    if strip_prefix and len(pt) > 32:
        pt = pt[32:]
    return pt.decode("utf-8", "replace")


def _decrypt(enc, key, strip_prefix=False):
    """Decrypt one Chrome cookie value (v10/v11 AES-128-CBC).

    strip_prefix drops the leading 32-byte SHA256(domain) that Chrome >= 130
    (schema version >= 24) prepends, instead of guessing from the bytes.
    """
    if isinstance(enc, str):
        enc = enc.encode("utf-8", "surrogateescape")
    if enc[:3] not in (b"v10", b"v11"):
        return enc.decode("utf-8", "replace")  # legacy plaintext entry
    return _decode_cookie(_aes_decrypt(enc[3:], key), strip_prefix)


def _decrypt_batch(enc_items, key, strip_prefix):
    """Decrypt [(name, enc), ...] with one openssl call.

    Raises CookieExtractionError if the batch is rejected — the caller then
    retries per value so one bad cookie cannot drop the rest of the profile.
    """
    plaintexts = _aes_decrypt_many([enc[3:] for _n, enc in enc_items], key)
    if plaintexts is None:
        raise CookieExtractionError("openssl batch decryption rejected input")
    return {
        name: _decode_cookie(pt, strip_prefix) for (name, _enc), pt in zip(enc_items, plaintexts)
    }


def _read_cookie_rows(db_path, host_patterns):
    """Copy the DB (plus WAL/SHM) so a running Chrome doesn't lock us out.

    Returns (rows, schema_version); Chrome >= 130 stores version >= 24 and
    prefixes each encrypted value with a 32-byte SHA256(domain).
    """
    tmp = tempfile.mkdtemp(prefix="si_ck_")
    try:
        for suffix in ("", "-wal", "-shm"):
            src = db_path + suffix
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(tmp, "Cookies" + suffix))
        con = sqlite3.connect(os.path.join(tmp, "Cookies"))
        try:
            where = " OR ".join("host_key LIKE ?" for _ in host_patterns)
            rows = con.execute(
                f"SELECT name, host_key, encrypted_value, value FROM cookies WHERE {where}",
                host_patterns,
            ).fetchall()
            try:
                ver = con.execute("SELECT value FROM meta WHERE key='version'").fetchone()
                version = int(ver[0]) if ver else 0
            except (sqlite3.Error, TypeError, ValueError):
                version = 0
            return rows, version
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def read_site_cookies(site, explicit=None):
    """Return {cookie_name: value} for ``site`` (a COOKIE_SITES key) from a
    logged-in profile.

    Tries each profile in order and returns the first that holds the site's
    session cookie. Raises CookieExtractionError with a reason if none work.
    """
    spec = COOKIE_SITES[site]
    session_cookie = spec["session_cookie"]
    label = spec["label"]
    errors = []
    for info in list_profiles(explicit):
        db = os.path.join(info["dir"], "Cookies")
        try:
            rows, version = _read_cookie_rows(db, spec["host_patterns"])
        except Exception as e:  # unreadable/corrupt profile — keep trying
            errors.append(f"{db}: {e}")
            continue
        if not rows:
            continue
        try:
            key = hashlib.pbkdf2_hmac("sha1", _password(info), b"saltysalt", info["iterations"], 16)
        except CookieExtractionError as e:
            errors.append(f"{info['dir']}: {e}")
            continue

        strip_prefix = version >= CHROME_COOKIE_SCHEMA_VERSION_V24
        cookies = {}
        enc_items = []  # v10/v11 rows → one openssl call for all of them
        for name, _host, enc, val in rows:
            # enc[:3] is bytes for BLOBs and str for TEXT; the `in` check
            # against bytes is False (no error) for TEXT, matching _decrypt.
            if enc and enc[:3] in (b"v10", b"v11"):
                enc_items.append((name, enc))
            elif enc:
                try:
                    cookies[name] = _decrypt(enc, key, strip_prefix)
                except CookieExtractionError as e:
                    errors.append(f"{info['dir']}: {name}: {e}")
            else:
                cookies[name] = val
        if enc_items:
            try:
                cookies.update(_decrypt_batch(enc_items, key, strip_prefix))
            except CookieExtractionError:
                # Batch rejected: retry per value so one malformed cookie
                # cannot drop a profile that holds the session.
                for name, enc in enc_items:
                    try:
                        cookies[name] = _decrypt(enc, key, strip_prefix)
                    except CookieExtractionError as e:
                        errors.append(f"{info['dir']}: {name}: {e}")
        if cookies.get(session_cookie):
            print(f"[+] Session found: {info['browser']} — {info['dir']}")
            return cookies

    if errors:
        raise CookieExtractionError(f"could not read a {label} session:\n  " + "\n  ".join(errors))
    raise CookieExtractionError(
        f"no {label} session cookie found — log in to {label} in Chrome first"
    )
