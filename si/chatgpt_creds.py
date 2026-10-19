"""ChatGPT.com credential load/save/extract.

Parallel to creds.py for claude.ai. The session token alone is enough: the
accessToken is derived from it via /api/auth/session and refreshed lazily,
so nothing time-sensitive is ever persisted.
"""

import os

from .compat import cffi_requests
from .config import (
    CHATGPT_ACCOUNT_ID_FALLBACK,
    CHATGPT_CRED_FILE,
    CHATGPT_IMPERSONATE,
    CHATGPT_UA,
    CHATGPT_URL_BASE,
)
from .jsonfile import read_json, write_private_json

# Cookie whose presence marks a logged-in chatgpt.com session.
SESSION_COOKIE = "__Secure-next-auth.session-token"

# Shards Chrome splits the session token into when it exceeds the 4096-byte
# per-cookie limit. Reassembled in numeric order.
_SHARD_NAMES = (SESSION_COOKIE, f"{SESSION_COOKIE}.1", f"{SESSION_COOKIE}.2", f"{SESSION_COOKIE}.3")


def reassemble_session_token(cookies):
    """Join sharded session-token cookies into one value.

    Chrome splits long cookies as ``name``, ``name.1``, ``name.2``... in the
    order they were written, so numeric order reconstructs the original. A
    single unsharded token is returned unchanged.
    """
    shards = []
    for i, name in enumerate(_SHARD_NAMES):
        val = cookies.get(name)
        if val:
            shards.append((i, val))
    if not shards:
        return ""
    shards.sort()
    return "".join(v for _i, v in shards)


class ChatGPTCredentials:
    """Holds the chatgpt.com session token, derived accessToken and account id."""

    def __init__(self):
        self.cookies = {}
        self.access_token = None
        self.account_id = None

    @property
    def session_token(self):
        return reassemble_session_token(self.cookies)

    # ── Persistence ──────────────────────────────────────

    def load(self):
        if not os.path.exists(CHATGPT_CRED_FILE):
            return False
        if os.name != "nt" and (os.stat(CHATGPT_CRED_FILE).st_mode & 0o077):
            os.chmod(CHATGPT_CRED_FILE, 0o600)
            print(f"[!] Tightened permissions on {CHATGPT_CRED_FILE} to user-only.")
        d = read_json(CHATGPT_CRED_FILE)
        if d is None:
            print(
                f"[!] ChatGPT credentials unreadable: {CHATGPT_CRED_FILE} — "
                "re-run --chatgpt --from-chrome"
            )
            return False
        cookies = d.get("cookies", {})
        if not isinstance(cookies, dict):
            print(
                f"[!] ChatGPT credentials unreadable: {CHATGPT_CRED_FILE} — "
                "re-run --chatgpt --from-chrome"
            )
            return False
        self.cookies = cookies
        self.account_id = d.get("account_id")
        return bool(self.session_token)

    def save(self):
        write_private_json(
            CHATGPT_CRED_FILE, {"cookies": self.cookies, "account_id": self.account_id}
        )
        print(f"[+] ChatGPT credentials saved → {CHATGPT_CRED_FILE}")

    def clear(self):
        self.cookies = {}
        self.access_token = None
        self.account_id = None
        if os.path.exists(CHATGPT_CRED_FILE):
            os.remove(CHATGPT_CRED_FILE)
            print(f"[+] ChatGPT credentials deleted: {CHATGPT_CRED_FILE}")
        else:
            print("[+] No ChatGPT credential file found.")

    def is_valid(self):
        return bool(self.session_token)

    # ── Acquisition ──────────────────────────────────────

    def from_chrome(self, profile=None):
        """Read the chatgpt.com session from a local Chrome profile (no re-login)."""
        from . import chrome_cookies

        print("[*] Reading chatgpt.com session from local Chrome profile...")
        try:
            cookies = chrome_cookies.read_site_cookies("chatgpt", profile)
        except chrome_cookies.CookieExtractionError as e:
            print(f"[!] {e}")
            return False
        if not reassemble_session_token(cookies):
            print(
                "[!] No __Secure-next-auth.session-token — log in to chatgpt.com in Chrome first."
            )
            return False
        self.cookies = cookies
        self.account_id = cookies.get("_account_id")
        return self._verify()

    def manual_input(self):
        print("\n" + "=" * 55)
        print("  Manual ChatGPT Credential Input")
        print("=" * 55)
        print("\n  1. Open https://chatgpt.com and log in")
        print("  2. DevTools (F12) → Application → Cookies → https://chatgpt.com")
        print("  3. Copy the '__Secure-next-auth.session-token' value")
        print("     (if Chrome split it, copy every '.1'/'.2' shard joined in order)\n")
        raw = input("  Session token: ").strip()
        self.cookies = {SESSION_COOKIE: raw}
        return self._verify()

    def _verify(self):
        """Resolve the accessToken + account id, proving the session works."""
        if not self.refresh_token():
            print(
                "[!] ChatGPT session rejected. Re-login in Chrome, then re-run --chatgpt --from-chrome."
            )
            return False
        print(f"[+] account id: {self.account_id or '(none)'}")
        self.save()
        return True

    # ── Token lifecycle ──────────────────────────────────

    def cookie_header(self):
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def make_headers(self, accept="*/*"):
        """Browser-plausible headers for chatgpt.com API calls.

        The originator/frontend pair identifies the Codex webview, which is
        what the internal /backend-api/f/* endpoints are gated on.
        """
        return {
            "User-Agent": CHATGPT_UA,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": CHATGPT_URL_BASE,
            "Referer": f"{CHATGPT_URL_BASE}/",
            "sec-ch-ua": '"Chromium";v="154", "Google Chrome";v="154", "Not A(Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "oai-did": self.cookies.get("oai-did", ""),
            "OAI-Language": "en-US",
            "originator": "Codex Browser",
            "x-openai-web-frontend": "codex_webview",
            "x-openai-codex-window-type": "browser",
            "Cookie": self.cookie_header(),
        }

    def refresh_token(self):
        """Exchange the session cookie for an accessToken.

        Returns True on success. The token is short-lived (~a few hours),
        so callers re-enter here whenever a request comes back 401.
        """
        headers = {
            "User-Agent": CHATGPT_UA,
            "Accept": "application/json",
            "Referer": f"{CHATGPT_URL_BASE}/",
            "Cookie": self.cookie_header(),
        }
        try:
            # Pin the chatgpt.com TLS fingerprint, not the claude.ai session's.
            resp = cffi_requests.get(
                f"{CHATGPT_URL_BASE}/api/auth/session",
                headers=headers,
                impersonate=CHATGPT_IMPERSONATE,
                timeout=20,
            )
        except Exception as e:
            print(f"[!] ChatGPT auth lookup failed: {e}")
            return False
        if resp is None or resp.status_code != 200:
            code = resp.status_code if resp is not None else "no response"
            print(f"[!] ChatGPT session invalid (HTTP {code}).")
            return False
        try:
            data = resp.json()
        except Exception:
            print("[!] ChatGPT auth lookup returned non-JSON.")
            return False
        token = data.get("accessToken")
        if not token:
            print("[!] ChatGPT session has no accessToken — not logged in.")
            return False
        self.access_token = token
        account = data.get("account") or {}
        self.account_id = account.get("id") or self.account_id or CHATGPT_ACCOUNT_ID_FALLBACK
        return True

    def auth_headers(self):
        """Authorization header pair, refreshing the accessToken if exhausted.

        ChatGPT access tokens carry an expiry the client cannot predict, so a
        401 is retried once through this path instead of caching a deadline.
        """
        if not self.access_token and not self.refresh_token():
            return None
        return {
            "Authorization": f"Bearer {self.access_token}",
            "ChatGPT-Account-Id": self.account_id or "",
        }
