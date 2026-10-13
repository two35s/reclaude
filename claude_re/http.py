"""HTTP layer: reused session, retries, conversation CRUD, model discovery."""
import random
import time

from .compat import HAS_CFFI, cffi_requests, std_requests
from .config import IMPERSONATE_TARGET, MODEL_ALIASES, MODEL_CACHE, MODEL_CACHE_TTL, UA, URL_BASE

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


def delete_conversation(creds, conv_id):
    """Delete a conversation so it doesn't appear in browser sidebar."""
    url = f"{URL_BASE}/api/organizations/{creds.org_id}/chat_conversations/{conv_id}"
    headers = {
        "User-Agent": UA,
        "Content-Type": "application/json",
        "Cookie": creds.cookie_header()
    }
    try:
        sess = get_session()
        if sess is not None:
            sess.delete(url, headers=headers, timeout=10)
        elif HAS_CFFI:
            cffi_requests.delete(url, headers=headers, impersonate=IMPERSONATE_TARGET, timeout=10)
        elif std_requests:
            std_requests.delete(url, headers=headers, timeout=10)
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
            sess = get_session()
            if sess is not None:
                if method == "GET":
                    resp = sess.get(url, headers=headers, timeout=timeout)
                else:
                    resp = sess.post(url, json=json_body, headers=headers,
                                     timeout=timeout)
            elif HAS_CFFI:
                if method == "GET":
                    resp = cffi_requests.get(url, headers=headers,
                                             impersonate=IMPERSONATE_TARGET,
                                             timeout=timeout)
                else:
                    resp = cffi_requests.post(url, json=json_body, headers=headers,
                                              impersonate=IMPERSONATE_TARGET,
                                              timeout=timeout)
            else:
                if method == "GET":
                    resp = std_requests.get(url, headers=headers, timeout=timeout)
                else:
                    resp = std_requests.post(url, json=json_body, headers=headers,
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


def fetch_available_models(creds, force=False):
    """Best-effort discovery of server-side models, cached for MODEL_CACHE_TTL.

    Tries the organizations endpoint (already used in auto_fetch) and merges
    any model identifiers found into MODEL_ALIASES. Never raises — returns
    the effective alias map (static + discovered). Static map is the source
    of truth when discovery yields nothing.
    """
    now = time.time()
    if not force and MODEL_CACHE["models"] is not None \
            and (now - MODEL_CACHE["ts"]) < MODEL_CACHE_TTL:
        return MODEL_CACHE["models"]
    effective = dict(MODEL_ALIASES)
    try:
        url = f"{URL_BASE}/api/organizations"
        headers = {"User-Agent": UA, "Cookie": creds.cookie_header(),
                   "Referer": "https://claude.ai/"}
        resp = fetch_with_retries("GET", url, headers=headers, timeout=10,
                                  max_retries=1)
        if resp is not None and resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                data = None
            found = []
            if isinstance(data, list):
                for org in data:
                    caps = (org.get("capabilities") or []) if isinstance(org, dict) else []
                    for c in caps:
                        if isinstance(c, str) and c.startswith("claude-"):
                            found.append(c)
            for m in found:
                if m not in effective.values() and m not in effective:
                    effective[m] = m
    except Exception:
        pass
    MODEL_CACHE["models"] = effective
    MODEL_CACHE["ts"] = now
    return effective
