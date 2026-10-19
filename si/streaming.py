"""SSE streaming + rate-limit handling."""

import json
import os
import random
import re
import sys
import time
import uuid

from . import http as http_mod
from .config import _CONFIG_DIR, DEFAULT_MODEL, MODEL_ALIASES, URL_BASE
from .payload import build_payload

# Rate limiting and retry constants
MAX_RATE_RETRIES = 2
MAX_JITTER = 1.5
DEFAULT_RETRY_WAIT = 30
MAX_WAIT_CAP = 300
REQUEST_TIMEOUT = http_mod.REQUEST_TIMEOUT


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


def stream_prompt(
    creds,
    prompt,
    model=DEFAULT_MODEL,
    stealth=True,
    session_state=None,
    _rate_retries=0,
    on_text=None,
    allow_fallback=True,
):
    """Send prompt with real-time streaming. Returns full response text.

    Session memory: uses ONE conversation for the entire session.
    If stealth=True, that conversation is deleted when the session ends (not per-prompt).
    session_state dict tracks: {'conv_id': str, 'created': bool}

    _rate_retries is internal: it threads the rate-limit retry budget through
    the 429 -> handle_rate_limit -> stream_prompt cycle so retries stay bounded.
    allow_fallback=False disables the silent haiku downgrade on 429 (proxy mode:
    the client owns retries and expects the model it asked for).
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
    endpoint = (
        f"{URL_BASE}/api/organizations/{creds.org_id}/chat_conversations/{conv_id}/completion"
    )
    headers = http_mod.make_headers(creds)

    def emit(txt):
        if on_text:
            on_text(txt)
        else:
            sys.stdout.write(txt)
            sys.stdout.flush()

    parts = []
    buffer = b""  # bytes: join chunks before decoding so UTF-8 never splits
    raw_body = []  # first chunks, kept only while no text has streamed
    error_body = ""
    status_code = [0]  # mutable for callback scope
    done = [False]

    def consume_data(data):
        """Handle one SSE data payload; True means stop consuming."""
        if data.strip() == "[DONE]":
            return True
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return False
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
            return True
        return False

    def on_chunk(chunk: bytes):
        nonlocal buffer
        if not parts:  # the error path only reads this before any text arrives
            raw_body.append(chunk)
        buffer, lines = _split_lines(buffer, chunk)
        for raw_line in lines:
            data = _sse_data(raw_line)
            if data is not None and consume_data(data):
                done[0] = True
                return

    # ── Send request (reused session + consistent fingerprint) ─
    resp = None
    try:
        resp = http_mod.request(
            "POST",
            endpoint,
            headers=headers,
            json_body=payload,
            timeout=REQUEST_TIMEOUT,
            content_callback=on_chunk,
        )
    except Exception as e:
        print(f"\n[!] Request error: {e}")

    # Transport failure with nothing streamed: return None, never "" — an
    # empty string reads to callers (proxy/MCP) as a successful blank reply.
    if resp is None and not parts:
        return None

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
                    elif (
                        "account_session_invalid" in low
                        or "invalid authorization" in low
                        or "authentication_error" in low
                    ):
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
            if (
                "account_session_invalid" in low
                or "invalid authorization" in low
                or "authentication_error" in low
            ):
                print("\n[!] Session invalid or expired. Run --auto-fetch")
            elif "model" in low:
                print(f"\n[!] Model '{model}' not available on your account tier.")
                print("    Try: /model sonnet")
            else:
                print(f"\n[!] 403: {error_body[:200]}")
        elif sc == 429:
            return handle_rate_limit(
                resp,
                creds,
                prompt,
                model,
                stealth,
                session_state,
                _retries=_rate_retries,
                on_text=on_text,
                allow_fallback=allow_fallback,
            )
        else:
            print(f"\n[!] HTTP {sc}: {error_body[:200]}")
        return None

    if not done[0]:
        print("\n[!] Stream ended before completion; discarding the partial reply.")
        return None

    full = "".join(parts)
    if on_text is None:  # only echo the trailing blank line in stdout-streaming mode
        print()

    # ── Save last response ───────────────────────────────
    if full:
        # Write-then-replace: two proxy threads writing the same path
        # directly would interleave; os.replace is atomic per swap.
        path = os.path.join(_CONFIG_DIR, "last_response.txt")
        tmp = f"{path}.{uuid.uuid4().hex[:8]}.tmp"  # per-write name; threads share the path
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(full)
        os.replace(tmp, path)
    return full


def handle_rate_limit(
    resp,
    creds,
    prompt,
    model,
    stealth,
    session_state=None,
    _retries=0,
    _max_retries=MAX_RATE_RETRIES,
    on_text=None,
    allow_fallback=True,
):
    """Handle 429: honor Retry-After, fallback to haiku once, bounded retries.

    Preserves session_state so conversation memory survives the retry.
    Adds jitter to avoid thundering-herd on shared Retry-After values.
    With allow_fallback=False the model is never swapped and no wait/retry
    happens here — the caller (proxy client) decides what to do.
    """
    if not allow_fallback:
        print("\n[!] Rate limited (429). Returning error; the client decides on retry.")
        return None
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
        return stream_prompt(
            creds,
            prompt,
            haiku_id,
            stealth,
            session_state,
            _rate_retries=_retries,
            on_text=on_text,
            allow_fallback=allow_fallback,
        )

    # Already on haiku — wait with jitter and retry (bounded)
    wait = DEFAULT_RETRY_WAIT
    try:
        wait = int(retry)
    except Exception:
        pass
    wait = min(wait, MAX_WAIT_CAP) + random.uniform(0, MAX_JITTER)
    print(f"[*] Waiting {wait:.1f}s (attempt {_retries + 1}/{MAX_RETRIES})...")
    time.sleep(wait)
    return stream_prompt(
        creds,
        prompt,
        model,
        stealth,
        session_state,
        _rate_retries=_retries + 1,
        on_text=on_text,
        allow_fallback=allow_fallback,
    )
