"""Runtime compat: UTF-8 stdio + optional HTTP backends."""

import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    from curl_cffi import requests as cffi_requests  # type: ignore

    HAS_CFFI = True
except ImportError:
    cffi_requests = None  # type: ignore
    HAS_CFFI = False

if not HAS_CFFI:
    # Raise instead of exiting: importing a library must not kill the
    # interpreter.  __main__ turns this into the friendly CLI message.
    raise ImportError("reclaude needs an HTTP backend: pip install curl_cffi")
