"""Shared constants, paths, models, payload template."""

import os
import sys

from .timezone import detect_timezone

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE = os.path.join(DIR, "claude_profile")

# Credential storage: outside repo in user config directory
if sys.platform == "win32":
    _CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "si")
    _OLD_CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "claude_re")
else:
    _CONFIG_DIR = os.path.expanduser("~/.config/si")
    _OLD_CONFIG_DIR = os.path.expanduser("~/.config/claude_re")
# One-time migration from the pre-rename location: stored sessions survive.
if not os.path.isdir(_CONFIG_DIR) and os.path.isdir(_OLD_CONFIG_DIR):
    try:
        os.rename(_OLD_CONFIG_DIR, _CONFIG_DIR)
    except OSError:
        pass  # fall back to re-running the credential command
os.makedirs(_CONFIG_DIR, mode=0o700, exist_ok=True)
if os.name != "nt":
    # mode above only applies at creation; tighten pre-existing dirs.
    os.chmod(_CONFIG_DIR, 0o700)
CRED_FILE = os.path.join(_CONFIG_DIR, "claude_session.json")
CHATGPT_CRED_FILE = os.path.join(_CONFIG_DIR, "chatgpt_session.json")

URL_BASE = "https://claude.ai"
CHATGPT_URL_BASE = "https://chatgpt.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
# Match TLS fingerprint to declared UA (Chrome 136) — avoids UA/TLS mismatch.
IMPERSONATE_TARGET = "chrome136"
DEFAULT_MODEL = "claude-sonnet-5-5"

# Model IDs mirror api anthropic naming (claude-<family>-<version>).
# Only entries verified against the live server belong here; unknown names
# still pass through unchanged. The server advertises the current generation
# (below) in its system prompt; per-account availability depends on the plan.
MODEL_ALIASES = {
    # ── Current generation (server-advertised) ─
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",  # default
    "opus": "claude-opus-5-5",  # pro/max
    "fable": "claude-fable-5",  # pro/specialized
    # ── Previous generation ────────────────────
    "sonnet-5": "claude-sonnet-5",
    "sonnet-4-6": "claude-sonnet-4-6",
    "haiku-4-5": "claude-haiku-4-5",
    # ── Dated snapshots ────────────────────────
    "haiku-snap": "claude-haiku-4-5-20251001",
}

# Cryptographic constants
PBKDF2_ITERATIONS_MAC = 1003
PBKDF2_ITERATIONS_LINUX = 1
AES_BLOCK_SIZE = 16
AES_KEY_SIZE = 16
AES_IV_SIZE = 16
CHROME_COOKIE_SCHEMA_VERSION_V24 = 24

# Sites whose sessions can be read from a Chrome profile.
# host: exact host_key values matched with SQL LIKE — the apex and the
# leading-dot domain form Chrome stores domain cookies under. Deliberately
# not infix patterns: '%openai.com%' also matches openai.com.evil.test,
# whose planted cookie could shadow the real one. chatgpt keeps legacy
# sessions on openai.com after the domain migration.
# cookie: the cookie whose presence marks a logged-in session.
COOKIE_SITES = {
    "claude": {
        "host_patterns": ("claude.ai", ".claude.ai"),
        "session_cookie": "sessionKey",
        "label": "claude.ai",
    },
    "chatgpt": {
        "host_patterns": ("chatgpt.com", ".chatgpt.com", "openai.com", ".openai.com"),
        "session_cookie": "__Secure-next-auth.session-token",
        "label": "chatgpt.com",
    },
}

# ChatGPT constants. TLS impersonation must match the declared UA, so both
# track the same Chrome generation.
CHATGPT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)
CHATGPT_IMPERSONATE = "chrome150"
# Last-resort account id: the session endpoint normally supplies it, but the
# sentinel headers require the header to be present even on anonymous plans.
# Set SI_CHATGPT_ACCOUNT_ID to your own; empty is accepted by the sentinel in
# practice (it appears to check presence, not ownership), but that is
# unverified for every account. Upgrade path: parse the id from the session
# JWT, or capture a sentinel request from an anonymous account.
CHATGPT_ACCOUNT_ID_FALLBACK = os.environ.get("SI_CHATGPT_ACCOUNT_ID", "")
CHATGPT_DEFAULT_MODEL = "auto"
# Models chatgpt.com's own selector advertises, in the order it lists them.
CHATGPT_MODELS = (
    "auto",
    "gpt-5-6",
    "gpt-5-3-mini",
    "gpt-5-6-mini",
    "gpt-5-6-t-mini",
    "gpt-5-6-t-mini-mini",
    "research",
    "gpt-6",
    "gpt-6-t-mini",
    "gpt-6-mini",
    "gpt-6-t-mini-mini",
)

# Sentinel proof-of-work: the answer must satisfy
# sha3_512(seed + answer)[:len(difficulty)] <= bytes.fromhex(difficulty).
# 500k is the ceiling the web client itself uses.
POW_MAX_NONCE = 500_000
# A captured Turnstile fallback is session data, not a constant: it lives in
# SI_CHATGPT_TURNSTILE or ~/.config/si/chatgpt_turnstile.txt (see chatgpt_stream).


def resolve_model(name):
    """Resolve shorthand or full model name."""
    if name in MODEL_ALIASES:
        return MODEL_ALIASES[name]
    return name  # pass through as-is


SYSTEM_TIMEZONE = detect_timezone()

PAYLOAD_TEMPLATE = {
    "prompt": "",
    "model": DEFAULT_MODEL,
    "timezone": SYSTEM_TIMEZONE,
    "locale": "en-US",
    "rendering_mode": "messages",
    "turn_message_uuids": {"human_message_uuid": "", "assistant_message_uuid": ""},
    "attachments": [],
    "files": [],
    "sync_sources": [],
}
