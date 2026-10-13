"""HTTP layer: reused session, retries, conversation CRUD."""
import random
import time

from .compat import HAS_CFFI, cffi_requests, std_requests
from .config import IMPERSONATE_TARGET, UA, URL_BASE

_HTTP_SESSION = None


def get_session():
    """Return a shared curl_cffi Session, or None if curl_cffi is missing."""
    global _HTTP_SESSION
    if _HTTP_SESSION is not None:
        return _HTTP_SESSION
    if HAS_CFFI:
        try:
            _HTTP_SESSION = cffi_requests.Session(impersonate=IMPERSONATE_TARGET)
            return _HTTP_SESSION
        except Exception:
            return None
    return None


def close_session():
    global _HTTP_SESSION
    try:
        if _HTTP_SESSION is not None:
            _HTTP_SESSION.close()
    except Exception:
        pass
    finally:
        _HTTP_SESSION = None


def make_headers(creds):
    return {
        "User-Agent": UA,
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "Referer": "https://claude.ai/",
        "Origin": "https://claude.ai",
        "Cookie": creds.cookie_header()
    }


def request(method, url, headers=None, json_body=None, timeout=15,
            content_callback=None):
    """Send one request through the best available backend.

    Picks the shared session, then curl_cffi, then plain requests. Raises on
    transport error; callers decide whether that is retryable.
    """
    sess = get_session()
    if sess is not None:
        if method == "GET":
            return sess.get(url, headers=headers, timeout=timeout)
        if method == "DELETE":
            return sess.delete(url, headers=headers, timeout=timeout)
        return sess.post(url, json=json_body, headers=headers, timeout=timeout,
                         content_callback=content_callback)
    if HAS_CFFI:
        if method == "GET":
            return cffi_requests.get(url, headers=headers,
                                     impersonate=IMPERSONATE_TARGET, timeout=timeout)
        if method == "DELETE":
            return cffi_requests.delete(url, headers=headers,
                                        impersonate=IMPERSONATE_TARGET, timeout=timeout)
        return cffi_requests.post(url, json=json_body, headers=headers,
                                  impersonate=IMPERSONATE_TARGET,
                                  content_callback=content_callback, timeout=timeout)
    if method == "GET":
        return std_requests.get(url, headers=headers, timeout=timeout)
    if method == "DELETE":
        return std_requests.delete(url, headers=headers, timeout=timeout)
    return std_requests.post(url, json=json_body, headers=headers, timeout=timeout)


def delete_conversation(creds, conv_id):
    """Delete a conversation so it doesn't appear in browser sidebar."""
    url = f"{URL_BASE}/api/organizations/{creds.org_id}/chat_conversations/{conv_id}"
    headers = {
        "User-Agent": UA,
        "Content-Type": "application/json",
        "Cookie": creds.cookie_header()
    }
    try:
        request("DELETE", url, headers=headers, timeout=10)
    except Exception:
        pass  # best-effort cleanup


def fetch_with_retries(method, url, headers=None, json_body=None, timeout=15,
                       max_retries=3):
    """Transient-error retry with exponential backoff + jitter.

    Retries only 429/5xx and network errors. Returns response or None.
    Caller handles 4xx semantics. Honors Retry-After on 429 when present.
    """
    last_resp = None
    for attempt in range(max_retries + 1):
        try:
            resp = request(method, url, headers=headers, json_body=json_body,
                           timeout=timeout)
        except Exception:
            resp = None
        if resp is None:
            pass  # network error — fall through to backoff
        elif resp.status_code < 500 and resp.status_code != 429:
            return resp
        else:
            last_resp = resp
            # Honor Retry-After on 429
            if resp.status_code == 429:
                try:
                    ra = resp.headers.get("Retry-After", "")
                    wait = int(ra) if ra else 2 ** attempt
                except Exception:
                    wait = 2 ** attempt
                wait = min(wait, 60) + random.uniform(0, 1)
                time.sleep(wait)
                continue
        if attempt < max_retries:
            time.sleep(min(2 ** attempt, 8) + random.uniform(0, 0.5))
    return last_resp


def create_conversation(creds, conv_id, model):
    """Create a new conversation on the server (required before posting completion).

    Returns True on success. On failure, prints the server's reason (e.g. an
    unsupported model or an expired session) so the caller can just bail out.
    """
    url = f"{URL_BASE}/api/organizations/{creds.org_id}/chat_conversations"
    headers = {
        "User-Agent": UA,
        "Content-Type": "application/json",
        "Referer": "https://claude.ai/",
        "Origin": "https://claude.ai",
        "Cookie": creds.cookie_header()
    }
    payload = {"name": "", "model": model, "uuid": conv_id}
    resp = fetch_with_retries("POST", url, headers=headers,
                              json_body=payload, timeout=15, max_retries=2)
    if resp is None:
        print(f"[!] Could not reach the server to start a conversation.")
        return False
    if resp.status_code in (200, 201):
        return True
    try:
        body = resp.text[:300]
    except Exception:
        body = ""
    low = body.lower()
    if "unsupported model" in low:
        print(f"[!] Model '{model}' is not available on your account (HTTP {resp.status_code}).")
        print("    Try: /model sonnet   (or another alias from the README)")
    elif resp.status_code in (401, 403):
        print("[!] Session invalid or expired. Run --from-chrome or --auto-fetch")
    else:
        print(f"[!] Could not create conversation (HTTP {resp.status_code}): {body[:160]}")
    return False

