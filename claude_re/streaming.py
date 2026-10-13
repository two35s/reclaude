"""SSE streaming + rate-limit handling."""
import json
import os
import random
import re
import sys
import time
import uuid

from . import http as http_mod
from .compat import HAS_CFFI
from .config import DEFAULT_MODEL, DIR, MODEL_ALIASES, URL_BASE
from .payload import build_payload


def _split_lines(buffer, chunk):
    """Append a chunk and return (remaining_buffer, complete_lines).

    Bytes are joined before splitting so a UTF-8 character split across two
    network chunks is decoded whole instead of as two replacement characters.
    """
    buffer += chunk
    lines = []
    while b"\n" in buffer:
        line, buffer = buffer.split(b"\n", 1)
        lines.append(line)
    return buffer, lines


def _sse_data(line):
    """Return the payload of one SSE 'data:' line, or None if it is not one."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    line = line.strip()
    if not line.startswith("data: "):
        return None
    return line[6:]


def stream_prompt(creds, prompt, model=DEFAULT_MODEL, stealth=True,
                  session_state=None, _rate_retries=0, on_text=None):
    """Send prompt with real-time streaming. Returns full response text.

    Session memory: uses ONE conversation for the entire session.
    If stealth=True, that conversation is deleted when the session ends (not per-prompt).
    session_state dict tracks: {'conv_id': str, 'created': bool}

    _rate_retries is internal: it threads the rate-limit retry budget through
    the 429 -> handle_rate_limit -> stream_prompt cycle so retries stay bounded.
    """

    # One server-side conversation per session, created on the first prompt.
    if session_state is None:
        session_state = {}
    if not session_state.get("conv_id"):
        conv_id = str(uuid.uuid4())
        if not http_mod.create_conversation(creds, conv_id, model):
            return None  # reason already printed by create_conversation
        session_state["conv_id"] = conv_id
        session_state["created"] = True
    conv_id = session_state["conv_id"]

    payload = build_payload(prompt, model)
    endpoint = (f"{URL_BASE}/api/organizations/{creds.org_id}"
                f"/chat_conversations/{conv_id}/completion")
    headers = http_mod.make_headers(creds)

    def emit(txt):
        if on_text:
            on_text(txt)
        else:
            sys.stdout.write(txt)
            sys.stdout.flush()

    parts = []
    buffer = b""  # bytes: join chunks before decoding so UTF-8 never splits
    raw_body = []  # capture everything for error detection
    error_body = ""
    status_code = [0]  # mutable for callback scope

    def on_chunk(chunk: bytes):
        nonlocal buffer
        raw_body.append(chunk)
        buffer, lines = _split_lines(buffer, chunk)
        for raw_line in lines:
            data = _sse_data(raw_line)
            if data is None:
                continue
            if data.strip() == "[DONE]":
                return
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            t = obj.get("type", "")
            if t == "content_block_delta":
                delta = obj.get("delta", {})
                if delta.get("type") == "text_delta":
                    txt = delta.get("text", "")
                    parts.append(txt)
                    emit(txt)
            elif t == "error":
                err = obj.get("error", {})
                print(f"\n[!] {err.get('message', str(obj))}")
            elif t == "message_stop":
                return

    # ── Send request (reused session + consistent fingerprint) ─
    resp = None
    try:
        resp = http_mod.request("POST", endpoint, headers=headers,
                                json_body=payload, timeout=120,
                                content_callback=on_chunk)
    except Exception as e:
        print(f"\n[!] Request error: {e}")

    # ── Handle non-streaming fallback ────────────────────
    if resp and not HAS_CFFI:
        if resp.status_code == 200:
            for line in resp.text.split("\n"):
                data = _sse_data(line)
                if data is None:
                    continue
                if data.strip() == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                t = obj.get("type", "")
                if t == "content_block_delta":
                    delta = obj.get("delta", {})
                    if delta.get("type") == "text_delta":
                        txt = delta.get("text", "")
                        parts.append(txt)
                        emit(txt)

    # ── Check status / detect errors ───────────────────────
    if resp:
        status_code[0] = resp.status_code

    # If callback got data but no SSE parts, check raw body for errors
    if not parts:
        full_raw = b"".join(raw_body).decode("utf-8", "replace")
        if full_raw:
            # Try parsing as JSON error
            try:
                err_obj = json.loads(full_raw.strip())
                if err_obj.get("type") == "error":
                    err = err_obj.get("error", {}) or {}
                    msg = err.get("message", str(err_obj))
                    code = err.get("error_code") or err.get("code") or ""
                    low = f"{code} {msg}".lower()
                    if status_code[0] == 429:
                        pass  # 429s fall through to handle_rate_limit() below
                    elif ("account_session_invalid" in low
                          or "invalid authorization" in low
                          or "authentication_error" in low):
                        print("\n[!] Session invalid or expired. Run --auto-fetch")
                        return None
                    elif "model" in code or "model" in msg.lower():
                        print(f"\n[!] Model '{model}' not available: {msg}")
                        return None
                    else:
                        print(f"\n[!] Error: {msg}")
                        return None
            except json.JSONDecodeError:
                pass
            error_body = full_raw[:500]
        elif resp and resp.status_code != 200:
            try:
                error_body = resp.text[:500]
            except Exception:
                pass

    # ── Error handling ───────────────────────────────────
    sc = status_code[0] or (resp.status_code if resp is not None else 0)
    if (error_body and not parts) or (sc in (401, 403, 429) and not parts):
        if "Just a moment" in error_body or "cloudflare" in error_body.lower():
            print("\n[!] Cloudflare blocked — run --auto-fetch again.")
        elif sc == 401:
            print("\n[!] Session expired. Run --auto-fetch")
        elif sc == 403:
            low = error_body.lower()
            if ("account_session_invalid" in low or "invalid authorization" in low
                    or "authentication_error" in low):
                print("\n[!] Session invalid or expired. Run --auto-fetch")
            elif "model" in low:
                print(f"\n[!] Model '{model}' not available on your account tier.")
                print("    Try: /model sonnet")
            else:
                print(f"\n[!] 403: {error_body[:200]}")
        elif sc == 429:
            return handle_rate_limit(resp, creds, prompt, model, stealth,
                                     session_state, _retries=_rate_retries,
                                     on_text=on_text)
        else:
            print(f"\n[!] HTTP {sc}: {error_body[:200]}")
        return None

    full = "".join(parts)
    print()

    # ── Save last response ───────────────────────────────
    if full:
        with open(os.path.join(DIR, "last_response.txt"), "w", encoding="utf-8") as f:
            f.write(full)
    return full


def handle_rate_limit(resp, creds, prompt, model, stealth,
                      session_state=None, _retries=0, _max_retries=2, on_text=None):
    """Handle 429: honor Retry-After, fallback to haiku once, bounded retries.

    Preserves session_state so conversation memory survives the retry.
    Adds jitter to avoid thundering-herd on shared Retry-After values.
    """
    MAX_RETRIES = _max_retries
    if _retries >= MAX_RETRIES:
        print(f"\n[!] Rate limited ({MAX_RETRIES} retries). Giving up — try later or /model haiku.")
        return None
    retry = "unknown"
    try:
        retry = resp.headers.get("Retry-After", "") if resp is not None else ""
        if not retry and resp is not None:
            body = resp.text[:500]
            m = re.search(r'"retry_after":\s*(\d+)', body)
            if m:
                retry = m.group(1)
    except Exception:
        pass

    print(f"\n[!] Rate limited. Retry after: {retry}s")

    # Auto-fallback to haiku if not already on it (single hop, keep session).
    # Carry the retry budget so a rate-limited haiku still terminates.
    haiku_id = MODEL_ALIASES.get("haiku", "claude-haiku-5-5")
    if model != haiku_id:
        print("[*] Falling back to haiku...")
        return stream_prompt(creds, prompt, haiku_id, stealth,
                             session_state, _rate_retries=_retries, on_text=on_text)

    # Already on haiku — wait with jitter and retry (bounded)
    wait = 30
    try:
        wait = int(retry)
    except Exception:
        pass
    wait = min(wait, 300) + random.uniform(0, 1.5)
    print(f"[*] Waiting {wait:.1f}s (attempt {_retries + 1}/{MAX_RETRIES})...")
    time.sleep(wait)
    return stream_prompt(creds, prompt, model, stealth, session_state,
                         _rate_retries=_retries + 1, on_text=on_text)
