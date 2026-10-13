"""Shared constants, paths, models, payload template."""
import os
import sys

from .timezone import detect_timezone

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE = os.path.join(DIR, "claude_profile")

# Credential storage: outside repo in user config directory
if sys.platform == "win32":
    _CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "claude_re")
else:
    _CONFIG_DIR = os.path.expanduser("~/.config/claude_re")
os.makedirs(_CONFIG_DIR, mode=0o700, exist_ok=True)
CRED_FILE = os.path.join(_CONFIG_DIR, "claude_session.json")

URL_BASE = "https://claude.ai"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
# Match TLS fingerprint to declared UA (Chrome 136) — avoids UA/TLS mismatch.
IMPERSONATE_TARGET = "chrome136"
DEFAULT_MODEL = "claude-sonnet-5-5"

# Model IDs mirror api anthropic naming (claude-<family>-<version>).
# Only entries verified against the live server belong here; unknown names
# still pass through unchanged. The server advertises the current generation
# (below) in its system prompt; per-account availability depends on the plan.
MODEL_ALIASES = {
    # ── Current generation (server-advertised) ─
    "haiku":        "claude-haiku-5-5",
    "sonnet":       "claude-sonnet-5-5",          # default
    "opus":         "claude-opus-5-5",            # pro/max
    "fable":        "claude-fable-5",             # pro/specialized
    # ── Previous generation ────────────────────
    "sonnet-5":     "claude-sonnet-5",
    "sonnet-4-6":   "claude-sonnet-4-6",
    "haiku-4-5":    "claude-haiku-4-5",
    # ── Dated snapshots ────────────────────────
    "haiku-snap":   "claude-haiku-4-5-20251001",
}


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
    "turn_message_uuids": {
        "human_message_uuid": "",
        "assistant_message_uuid": ""
    },
    "attachments": [],
    "files": [],
    "sync_sources": []
}

