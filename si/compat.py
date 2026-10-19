"""Runtime compat: UTF-8 stdio + the required HTTP backend."""

import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    from curl_cffi import requests as cffi_requests  # type: ignore
except ImportError as e:
    # Raise instead of exiting: importing a library must not kill the
    # interpreter.  __main__ turns this into the friendly CLI message.
    raise ImportError("si needs an HTTP backend: pip install curl_cffi") from e

__all__ = ("cffi_requests",)
