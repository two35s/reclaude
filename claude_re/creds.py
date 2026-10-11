"""Credential load/save/extract/clear."""
import json
import os
import re
import stat
import sys
import uuid

from . import chrome as chrome_mod
from .config import CRED_FILE


class CredentialManager:
    """Manages Claude.ai session credentials (org_id, conv_id, cookies)."""

    def __init__(self):
        self.org_id = None
        self.conv_id = None
        self.cookies = {}
        self.session_key = None

    # ── Persistence ──────────────────────────────────────

    def load(self):
        if not os.path.exists(CRED_FILE):
            return False
        with open(CRED_FILE) as f:
            d = json.load(f)
        self.org_id      = d.get("org_id")
        self.conv_id     = d.get("conv_id")
        self.session_key = d.get("session_key")
        self.cookies     = d.get("cookies", {})
        return bool(self.org_id and self.session_key)

    def save(self):
        with open(CRED_FILE, "w") as f:
            json.dump({"org_id": self.org_id, "conv_id": self.conv_id,
                       "session_key": self.session_key,
                       "cookies": self.cookies}, f, indent=2)
        # Restrict file permissions (user-only read/write)
        try:
            if sys.platform != "win32":
                os.chmod(CRED_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 600
        except Exception:
            pass
        print(f"[+] Credentials saved → {CRED_FILE}")

    def clear(self):
        """Wipe credentials from disk and memory."""
        self.org_id = None
        self.conv_id = None
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
        print("  3. URL has /organizations/{org_id}/chat_conversations/{conv_id}/")
        print("  4. Copy the Cookie header value\n")

        self.org_id = input("  org_id: ").strip()
        self.conv_id = input("  conv_id (or 'new'): ").strip()
        if self.conv_id.lower() == "new":
            self.conv_id = str(uuid.uuid4())
            print(f"  [+] Generated conv_id: {self.conv_id}")

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

    # ── Auto-fetch via real Chrome + CDP ───────────────────

    def auto_fetch(self):
        """Launch REAL Chrome → user passes CF & logs in → extract creds via CDP → close Chrome."""
        chrome = chrome_mod.find_chrome()
        if not chrome:
            print("[!] Chrome not found. Use --manual instead.")
            return False

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("[!] pip install playwright")
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
        print("    4. Copy the chat URL from address bar")
        input("\n  Press ENTER when done (keep Chrome open)...")

        url_input = input("  Paste the chat URL (or press Enter to skip): ").strip()
        if url_input:
            m = re.search(r'/chat/([a-f0-9\-]+)', url_input)
            if m:
                self.conv_id = m.group(1)
                print(f"[+] conv_id: {self.conv_id}")

        # Connect via CDP to extract cookies + org_id
        print("[*] Connecting to Chrome via CDP to extract cookies...")
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
            ctx = browser.contexts[0]

            # Extract cookies
            browser_cookies = ctx.cookies("https://claude.ai")
            self.cookies = {}
            for c in browser_cookies:
                self.cookies[c["name"]] = c["value"]
                if c["name"] == "sessionKey":
                    self.session_key = c["value"]

            # Extract org_id by calling Claude's API from the browser page
            for page in ctx.pages:
                if "claude.ai" not in page.url:
                    continue
                try:
                    orgs = page.evaluate("""
                        async () => {
                            try {
                                const r = await fetch('/api/organizations');
                                const data = await r.json();
                                return data;
                            } catch(e) { return null; }
                        }
                    """)
                    if orgs and isinstance(orgs, list) and len(orgs) > 0:
                        self.org_id = orgs[0].get("uuid") or orgs[0].get("id")
                        print(f"[+] org_id: {self.org_id}")
                        break
                except Exception:
                    pass

            # Fallback conv_id from page URL
            if not self.conv_id:
                for page in ctx.pages:
                    m = re.search(r'/chat/([a-f0-9\-]+)', page.url)
                    if m:
                        self.conv_id = m.group(1)
                        break

            browser.close()
        except Exception as e:
            print(f"[!] CDP connection failed: {e}")
            print("    Make sure Chrome is still open.")
            pw.stop()
            proc.terminate()
            return False

        pw.stop()

        if not self.conv_id:
            self.conv_id = str(uuid.uuid4())

        # Done — close Chrome
        print("[*] Closing Chrome...")
        proc.terminate()

        if self.session_key:
            print(f"[+] Session key: {self.session_key[:25]}...")
            self.save()
            return True
        print("[!] No sessionKey cookie found")
        return False

    # ── Helpers ──────────────────────────────────────────

    def cookie_header(self):
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def is_valid(self):
        return bool(self.org_id and self.session_key)
