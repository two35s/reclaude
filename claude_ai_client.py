#!/usr/bin/env python3
"""Backward-compat entry point. Real code lives in the `claude_re` package."""
from claude_re.chrome import do_login, find_chrome as _find_chrome, get_free_port as _get_free_port, launch_real_chrome as _launch_real_chrome
from claude_re.cli import main
from claude_re.compat import HAS_CFFI, cffi_requests, std_requests
from claude_re.config import (
    CRED_FILE,
    DEFAULT_MODEL,
    DIR,
    IMPERSONATE_TARGET,
    MODEL_ALIASES,
    MODEL_CACHE_TTL,
    PAYLOAD_TEMPLATE,
    PROFILE,
    SYSTEM_TIMEZONE,
    UA,
    URL_BASE,
    resolve_model,
)
from claude_re.config import MODEL_CACHE as _MODEL_CACHE
from claude_re.creds import CredentialManager
from claude_re.http import (
    close_session as _close_session,
    create_conversation as _create_conversation,
    delete_conversation as _delete_conversation,
    fetch_available_models,
    fetch_with_retries as _fetch_with_retries,
    get_session as _get_session,
    make_headers as _make_headers,
)
from claude_re.payload import build_payload
from claude_re.repl import print_help as _print_help, print_models as _print_models
from claude_re.streaming import handle_rate_limit as _handle_rate_limit, stream_prompt
from claude_re.timezone import detect_timezone as _detect_timezone

__all__ = [
    "CredentialManager",
    "build_payload",
    "fetch_available_models",
    "stream_prompt",
    "resolve_model",
    "do_login",
    "main",
]

if __name__ == "__main__":
    main()
