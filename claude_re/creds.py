"""Credential load/save/extract/clear."""
import json
import os

from . import chrome as chrome_mod
from .config import CRED_FILE, UA, URL_BASE
from .http import DEFAULT_TIMEOUT


class CredentialManager:
    """Manages Claude.ai session credentials (org_id, cookies)."""

    def __init__(self):
        self.org_id = None
        self.cookies = {}
        self.session_key = None

    # ── Persistence ──────────────────────────────────────

    def load(self):
        if not os.path.exists(CRED_FILE):
            return False
        with open(CRED_FILE) as f:
            d = json.load(f)
        self.org_id      = d.get("org_id")
        self.session_key = d.get("session_key")
        self.cookies     = d.get("cookies", {})
        return bool(self.org_id and self.session_key)

    def save(self):
        # Create the file already user-only (0600), never a wider window.
        fd = os.open(CRED_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"org_id": self.org_id, "session_key": self.session_key,
                       "cookies": self.cookies}, f, indent=2)
        print(f"[+] Credentials saved → {CRED_FILE}")

    def clear(self):
        """Wipe credentials from disk and memory."""
        self.org_id = None
        self.session_key = None
        self.cookies = {}
        if os.path.exists(CRED_FILE):
            os.remove(CRED_FILE)
            print(f"[+] Credentials deleted: {CRED_FILE}")
        else:
            print("[+] No credential file found.")

    # ── Manual input ─────────────────────────────────────

    def manual_input(self):
        print("\n" + "=" * 55)
        print("  Manual Credential Input")
        print("=" * 55)
        print("\n  Steps:")
        print("  1. Open https://claude.ai — send a message")
        print("  2. DevTools (F12) → Network → find 'completion' request")
        print("  3. URL has /organizations/{org_id}/ — copy that org_id")
        print("  4. Copy the Cookie header value\n")

        self.org_id = input("  org_id: ").strip()
        raw = input("  Cookie header: ").strip()
        self._parse_cookies(raw)
        self.save()

    def _parse_cookies(self, raw):
        self.cookies = {}
        for part in raw.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                self.cookies[k.strip()] = v.strip()
        self.session_key = self.cookies.get("sessionKey", "")
        if not self.session_key:
            print("[!] Warning: no sessionKey found in cookies")

    # ── Auto-fetch via real Chrome + CDP (stdlib, no playwright) ─

    def auto_fetch(self):
        """Launch REAL Chrome → user passes CF & logs in → extract creds via CDP → close Chrome."""
        from . import cdp as cdp_mod

        chrome = chrome_mod.find_chrome()
        if not chrome:
            print("[!] Chrome not found. Use --manual instead.")
            return False

        print("\n  ⚠  WARNING: This uses your authenticated browser session.")
        print("     Extracted credentials are sensitive — treat as passwords.")

        print("\n" + "=" * 55)
        print("  AUTO-FETCH: Real Chrome will launch.")
        print("  1. Cloudflare will pass automatically (real browser)")
        print("  2. Log in to Claude.ai if needed")
        print("  3. Open any chat and send a message")
        print("  4. Press ENTER here when done (don't close Chrome yet)")
        print("=" * 55)
        input("\n  Press ENTER to launch Chrome...")

        proc, cdp_port = chrome_mod.launch_real_chrome(chrome, "https://claude.ai")
        if not proc:
            return False

        print("\n[*] Chrome is open. Complete these steps:")
        print("    1. Pass Cloudflare (automatic in real Chrome)")
        print("    2. Log in if needed")
        print("    3. Open a chat and send any message")
        input("\n  Press ENTER when done (keep Chrome open)...")

        # Connect via CDP to extract cookies + org_id (stdlib client)
        print("[*] Connecting to Chrome via CDP to extract cookies...")
        try:
            cookies = cdp_mod.extract_session(cdp_port)
        except Exception as e:
            print(f"[!] CDP connection failed: {e}")
            print("    Make sure Chrome is still open.")
            proc.terminate()
            return False

        self.cookies = {}
        for name, value in cookies.items():
            self.cookies[name] = value
            if name == "sessionKey":
                self.session_key = value

        # Extract org_id by calling Claude's API from the browser page
        try:
            orgs = cdp_mod.fetch_orgs(cdp_port)
            if orgs and isinstance(orgs, list) and len(orgs) > 0:
                self.org_id = orgs[0].get("uuid") or orgs[0].get("id")
                print(f"[+] org_id: {self.org_id}")
        except Exception:
            pass

        # Done — close Chrome
        print("[*] Closing Chrome...")
        proc.terminate()

        if self.session_key:
            print(f"[+] Session key: {self.session_key[:25]}...")
            self.save()
            return True
        print("[!] No sessionKey cookie found")
        return False

    # ── From an already-logged-in Chrome profile ─────────

    def from_chrome(self, profile=None):
        """Read claude.ai cookies from a local Chrome profile (no re-login).

        `profile` may be a profile dir, a browser root, or a path to a
        Cookies file; None auto-discovers Chrome/Chromium/Brave/Edge.
        """
        from . import chrome_cookies

        print("[*] Reading claude.ai session from local Chrome profile...")
        try:
            cookies = chrome_cookies.read_claude_cookies(profile)
        except chrome_cookies.CookieExtractionError as e:
            print(f"[!] {e}")
            return False

        self.cookies = cookies
        self.session_key = cookies.get("sessionKey")
        self.org_id = cookies.get("lastActiveOrg")

        if not self.session_key:
            print("[!] No sessionKey cookie — log in to claude.ai in Chrome first.")
            return False

        if not self.org_id:
            print("[*] No lastActiveOrg cookie; asking the API for your org id...")
            self.org_id = self._fetch_org_id()
        if not self.org_id:
            print("[!] Could not determine org_id (session may be anonymous).")
            return False

        print(f"[+] org_id:      {self.org_id}")
        print(f"[+] session key: {self.session_key[:25]}...")
        self.save()
        return True

    def _fetch_org_id(self):
        """Best-effort org lookup using the extracted cookies."""
        try:
            from . import http as http_mod
            headers = {"User-Agent": UA, "Cookie": self.cookie_header(),
                       "Referer": "https://claude.ai/"}
            resp = http_mod.fetch_with_retries(
                "GET", f"{URL_BASE}/api/organizations", headers=headers,
                timeout=DEFAULT_TIMEOUT, max_retries=1)
            if resp is not None and resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    return data[0].get("uuid") or data[0].get("id")
        except Exception:
            pass
        return None

    # ── Helpers ──────────────────────────────────────────

    def cookie_header(self):
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def is_valid(self):
        return bool(self.org_id and self.session_key)
