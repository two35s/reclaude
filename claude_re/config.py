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
os.makedirs(_CONFIG_DIR, exist_ok=True)
CRED_FILE = os.path.join(_CONFIG_DIR, "claude_session.json")

URL_BASE = "https://claude.ai"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
# Match TLS fingerprint to declared UA (Chrome 136) — avoids UA/TLS mismatch.
IMPERSONATE_TARGET = "chrome136"
DEFAULT_MODEL = "claude-sonnet-4-6"

# Verified models (tested against claude.ai endpoint 2026-04-24)
# FREE = works on free tier, PRO = needs Pro/Max subscription
MODEL_ALIASES = {
    # ── Current generation (FREE) ─────────────
    "haiku":        "claude-haiku-4-5",
    "sonnet":       "claude-sonnet-4-6",          # latest, default
    "sonnet-4-5":   "claude-sonnet-4-5",          # previous sonnet
    # ── Dated snapshots (FREE) ────────────────
    "haiku-snap":   "claude-haiku-4-5-20251001",
    "sonnet-snap":  "claude-sonnet-4-5-20250929",
    # ── Premium (PRO/MAX only) ────────────────
    "opus":         "claude-opus-4-7",
    "opus-3":       "claude-3-opus-20240229",
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

# Discovered-model cache (TTL) — merged over static MODEL_ALIASES.
MODEL_CACHE = {"ts": 0.0, "models": None}
MODEL_CACHE_TTL = 3600
