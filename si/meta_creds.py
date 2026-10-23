"""Meta AI credential load/save/extract.

Parallel to chatgpt_creds.py for meta.ai. The session cookie (ecto_1_sess)
is enough; an accessToken is fetched lazily over the WS flow.
"""

import hashlib
import os
import re
import time

from .compat import cffi_requests
from .config import (
    META_CRED_FILE,
    META_IMPERSONATE,
    META_UA,
    META_URL_BASE,
)
from .jsonfile import read_json, write_private_json

SESSION_COOKIE = "ecto_1_sess"
LEGACY_SESSION_COOKIE = "abra_sess"

# Meta access tokens live ~5 minutes; refresh a little early.
_TOKEN_TTL = 4 * 60
_TOKEN_CACHE = {}
_TOKEN_LOCK = __import__("threading").Lock()


class MetaCredentials:
    """Holds the meta.ai session cookie and optional access token."""

    def __init__(self):
        self.cookies = {}
        self.access_token = None

    @property
    def session_cookie(self):
        for name in (SESSION_COOKIE, LEGACY_SESSION_COOKIE):
            if name in self.cookies and self.cookies[name]:
                return self.cookies[name]
        # fallback: some flows rely on datr/other cookies during auth
        return ""

    def load(self):
        if not os.path.exists(META_CRED_FILE):
            return False
        if os.name != "nt" and (os.stat(META_CRED_FILE).st_mode & 0o077):
            os.chmod(META_CRED_FILE, 0o600)
            print(f"[!] Tightened permissions on {META_CRED_FILE} to user-only.")
        d = read_json(META_CRED_FILE)
        if d is None:
            print(
                f"[!] Meta credentials unreadable: {META_CRED_FILE} — re-run --meta --from-chrome"
            )
            return False
        cookies = d.get("cookies", {})
        if not isinstance(cookies, dict):
            print(
                f"[!] Meta credentials unreadable: {META_CRED_FILE} — re-run --meta --from-chrome"
            )
            return False
        self.cookies = cookies
        return bool(self.session_cookie)

    def save(self):
        # The access token lives ~5 minutes, so it is never persisted.
        write_private_json(META_CRED_FILE, {"cookies": self.cookies})
        print(f"[+] Meta credentials saved → {META_CRED_FILE}")

    def clear(self):
        self.cookies = {}
        self.access_token = None
        if os.path.exists(META_CRED_FILE):
            os.remove(META_CRED_FILE)
            print(f"[+] Meta credentials deleted: {META_CRED_FILE}")
        else:
            print("[+] No Meta credential file found.")

    def is_valid(self):
        return bool(self.cookies)

    def from_chrome(self, profile=None):
        from . import chrome_cookies

        print("[*] Reading meta.ai session from local Chrome profile...")
        try:
            cookies = chrome_cookies.read_site_cookies("meta", profile)
        except chrome_cookies.CookieExtractionError as e:
            # Fallback: read any meta.ai cookies even if session cookie missing
            cookies = {}
            try:
                for info in chrome_cookies.list_profiles(profile):
                    db = os.path.join(info["dir"], "Cookies")
                    try:
                        rows, version = chrome_cookies._read_cookie_rows(
                            db, chrome_cookies.COOKIE_SITES["meta"]["host_patterns"]
                        )
                    except Exception:
                        continue
                    strip_prefix = version >= chrome_cookies.CHROME_COOKIE_SCHEMA_VERSION_V24
                    buckets = {}
                    for name, _host, enc, val in rows:
                        if enc and isinstance(enc, str):
                            enc = enc.encode("utf-8", "surrogateescape")
                        if enc and enc[:3] in (b"v10", b"v11"):
                            buckets.setdefault(enc[:3], []).append((name, enc))
                        elif enc:
                            cookies[name] = enc.decode("utf-8", "replace")
                        else:
                            cookies[name] = val
                    for prefix, items in buckets.items():
                        try:
                            secret = chrome_cookies._secret_for(info, prefix)
                        except Exception:
                            continue
                        if secret is None:
                            continue
                        key = __import__("hashlib").pbkdf2_hmac(
                            "sha1", secret, b"saltysalt", info["iterations"], 16
                        )
                        try:
                            cookies.update(chrome_cookies._decrypt_batch(items, key, strip_prefix))
                        except Exception:
                            for name, enc in items:
                                try:
                                    cookies[name] = chrome_cookies._decrypt(enc, key, strip_prefix)
                                except Exception:
                                    pass
                    if cookies:
                        break
            except Exception:
                pass
            if not cookies:
                print(f"[!] {e}")
                return False
        if cookies:
            self.cookies = cookies
            # Try to verify, but save anyway so models appear in UI
            if self._verify():
                return True
            # Save even if verification fails so models show up
            self.save()
            print(
                "[!] Meta credentials saved but may need valid session (log in to meta.ai fully)."
            )
            return True
        print("[!] No meta.ai cookies found — log in to meta.ai in Chrome first.")
        return False

    def manual_input(self):
        import os

        # allow non-interactive input
        for env in ("SI_META_ECTO_1_SESS", "META_AI_ECTO_1_SESS", "ECTO_1_SESS"):
            v = os.environ.get(env)
            if v and v.strip():
                self.cookies = {SESSION_COOKIE: v.strip()}
                return self._verify()
        for env in ("SI_META_COOKIES", "META_AI_COOKIES"):
            v = os.environ.get(env)
            if v and v.strip():
                # parse cookie header
                cookies = {}
                for part in v.split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, val = part.split("=", 1)
                        cookies[k.strip()] = val.strip()
                if cookies:
                    self.cookies = cookies
                    return self._verify()
        print("\n" + "=" * 55)
        print("  Manual Meta Credential Input")
        print("=" * 55)
        print("\n  1. Open https://www.meta.ai and log in")
        print("  2. DevTools (F12) → Application → Cookies → https://www.meta.ai")
        print("  3. Copy the 'ecto_1_sess' value (or the full Cookie header)\n")
        try:
            raw = input("  ecto_1_sess or Cookie header: ").strip()
        except EOFError:
            print("[!] No input provided. Set SI_META_COOKIES or SI_META_ECTO_1_SESS.")
            return False
        if "=" in raw and ";" in raw:
            cookies = {}
            for part in raw.split(";"):
                part = part.strip()
                if "=" in part:
                    k, val = part.split("=", 1)
                    cookies[k.strip()] = val.strip()
            self.cookies = cookies
        else:
            self.cookies = {SESSION_COOKIE: raw}
        return self._verify()

    def _verify(self):
        if not self.refresh_token():
            print(
                "[!] Meta session rejected. Re-login in Chrome, then re-run --meta --from-chrome."
            )
            return False
        self.save()
        return True

    def cookie_header(self):
        parts = []
        for k, v in self.cookies.items():
            if v:
                parts.append(f"{k}={v}")
        return "; ".join(parts)

    def make_headers(self, accept="*/*"):
        return {
            "User-Agent": META_UA,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": META_URL_BASE,
            "Referer": f"{META_URL_BASE}/",
            "Cookie": self.cookie_header(),
        }

    def refresh_token(self):
        """Fetch accessToken (ecto1:...) from meta.ai page, solving its JS challenge."""
        # Keyed by the session cookie: rd_challenge rotates but the token does not.
        cache_key = hashlib.sha256(self.session_cookie.encode()).hexdigest()[:16]
        with _TOKEN_LOCK:
            cached = _TOKEN_CACHE.get(cache_key)
            if cached and cached[1] > time.time():
                self.access_token = cached[0]
                return True
        headers = {
            "User-Agent": META_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cookie": self.cookie_header(),
        }
        try:
            sess = cffi_requests.Session(impersonate=META_IMPERSONATE)
        except Exception:
            sess = None
        try:
            resp = self._get(sess, headers)
            if resp is not None and resp.status_code == 403:
                m = re.search(r"fetch\('(/__rd_verify_[^']+)'", resp.text or "")
                if m:
                    resp2 = self._post(sess, m.group(1), headers)
                    if resp2 is not None:
                        self.cookies.update(resp2.cookies.get_dict())
                        headers["Cookie"] = self.cookie_header()
                        resp = self._get(sess, headers)
        except Exception as e:
            print(f"[!] Meta auth lookup failed: {e}")
            return False
        if resp is None or resp.status_code != 200:
            code = resp.status_code if resp is not None else "no response"
            print(f"[!] Meta session invalid (HTTP {code}).")
            return False
        m = re.search(r'accessToken[\\"]+:\s*[\\"]+(ecto1:[A-Za-z0-9_-]+)[\\"]+', resp.text)
        if not m:
            print("[!] Meta page has no accessToken — not logged in or challenge failed.")
            return False
        self.access_token = m.group(1)
        with _TOKEN_LOCK:
            _TOKEN_CACHE[cache_key] = (self.access_token, time.time() + _TOKEN_TTL)
        return True

    def _get(self, sess, headers):
        if sess is not None:
            return sess.get(f"{META_URL_BASE}/", headers=headers, timeout=30)
        return cffi_requests.get(
            f"{META_URL_BASE}/", headers=headers, impersonate=META_IMPERSONATE, timeout=30
        )

    def _post(self, sess, path, headers):
        url = f"{META_URL_BASE}{path}"
        if sess is not None:
            return sess.post(url, headers=headers, timeout=30)
        return cffi_requests.post(url, headers=headers, impersonate=META_IMPERSONATE, timeout=30)

    def auth_token(self):
        # Always go through refresh_token: its 4-minute cache makes this cheap
        # and a previously stored token would be stale by the next run.
        if not self.refresh_token():
            return None
        return self.access_token
