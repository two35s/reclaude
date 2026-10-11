"""Runtime compat: UTF-8 stdio + optional HTTP backends."""
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    cffi_requests = None  # type: ignore
    HAS_CFFI = False

try:
    import requests as std_requests
except ImportError:
    std_requests = None  # type: ignore

if not HAS_CFFI and not std_requests:
    print("[!] pip install curl_cffi")
    sys.exit(1)
