"""ChatGPT.com streaming backend (pure HTTP, no browser).

Wire format pinned against the live Codex webview surface, which differs from
chatgpt.com's own composer:

    POST /backend-api/sentinel/chat-requirements/prepare   {p}
        -> {prepare_token, proofofwork: {seed, difficulty}, turnstile}
    POST /backend-api/sentinel/chat-requirements/finalize
        {prepare_token, proofofwork, turnstile} -> {token}
    POST /backend-api/f/conversation       (SSE, needs the sentinel headers)

The proof-of-work answer is a base64 fingerprint array whose 4th field is the
nonce; the server accepts it when sha3_512(seed + answer) starts with at most
the difficulty's hex value. The turnstile blob is replayed — chatgpt.com does
not re-mint one per turn, so a single captured blob serves the session.

Like the claude backend, one server-side conversation backs a whole session and
is deleted afterwards in stealth mode. Unlike claude, the assistant text arrives
in one snapshot delta rather than token-by-token.
"""

import base64
import hashlib
import json
import os
import sys
import threading
import time
import uuid

from .compat import cffi_requests
from .config import (
    _CONFIG_DIR,
    CHATGPT_DEFAULT_MODEL,
    CHATGPT_IMPERSONATE,
    CHATGPT_URL_BASE,
    POW_MAX_NONCE,
)
from .timezone import detect_timezone

FINGERPRINT_PREFIX = "gAAAAA"  # fixed envelope every sentinel blob carries
POW_ANSWER_PREFIX = "gAAAAAB"
POW_ANSWER_SUFFIX = "~S"

# Captured from the Codex webview. A plausible desktop fingerprint beats a
# minimal one here: the array is what the server fingerprints the client on.
# shortcut: fields are not re-derived from real browser state, so the values
# are static apart from the timestamp, nonce and ids.
_FINGERPRINT_TEMPLATE = [
    40,
    "",
    4395630592,
    0,  # nonce, filled per token
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
    ),
    None,
    "bfc3a09a49ef7874df485c8f0020f40819fb44fb",
    "en-US",
    "en-US,en",
    2,
    "appVersion−5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "location",
    "styleMedia",
    3211.699999988079,
    "",
    "",
    4,
    0.0,  # epoch ms, filled per token
    0,
    0,
    0,
    0,
    0,
    0,
    0,
]

# ── Sentinel proof-of-work ──────────────────────────────────────────────────


def _time_string():
    # e.g. "Mon Oct 19 2026 10:05:47 GMT+0000 (Coordinated Universal Time)"
    return time.strftime("%a %b %d %Y %H:%M:%S GMT+0000", time.gmtime()) + " (UTC)"


def build_fingerprint(nonce=0, now_ms=None):
    """Return the 25-field fingerprint array the sentinel blobs carry."""
    arr = list(_FINGERPRINT_TEMPLATE)
    arr[1] = _time_string()
    arr[3] = nonce
    arr[14] = str(uuid.uuid4())
    arr[17] = float(now_ms if now_ms is not None else time.time() * 1000)
    return arr


def encode_blob(arr):
    """Encode a fingerprint array the way the web client does.

    The array body is base64'd inside a fixed envelope; the 4th field is the
    proof-of-work nonce, so the same encoder serves both the client challenge
    (nonce 0) and the solved answer.
    """
    raw = json.dumps(arr, separators=(",", ":"), ensure_ascii=False).encode()
    return FINGERPRINT_PREFIX + base64.b64encode(raw).decode()


def solve_pow(seed, difficulty, template=None):
    """Brute-force the nonce for a server challenge.

    Returns the answer token, or None when the difficulty is unreachable within
    the client's own nonce ceiling.
    """
    if not seed or not difficulty:
        return None
    try:
        target = bytes.fromhex(difficulty)
    except ValueError:
        return None
    prefix_len = len(difficulty)
    seed_bytes = seed.encode()
    base = list(template or build_fingerprint())
    for nonce in range(POW_MAX_NONCE):
        base[3] = nonce
        raw = json.dumps(base, separators=(",", ":"), ensure_ascii=False).encode()
        answer = base64.b64encode(raw)
        if hashlib.sha3_512(seed_bytes + answer).digest()[:prefix_len] <= target:
            return POW_ANSWER_PREFIX + answer.decode() + POW_ANSWER_SUFFIX
    return None


# ── HTTP plumbing ───────────────────────────────────────────────────────────


def _get_session():
    """Shared curl_cffi session pinned to the ChatGPT TLS/UA fingerprint.

    Separate from http.get_session(): that one impersonates the Chrome version
    claude.ai expects, and mixing the two fingerprints would show up as a
    UA/TLS mismatch to the sentinel checks.
    """
    global _CHATGPT_SESSION
    if _CHATGPT_SESSION is not None:
        return _CHATGPT_SESSION
    with _SESSION_LOCK:  # same double-check as http.get_session
        if _CHATGPT_SESSION is not None:
            return _CHATGPT_SESSION
        try:
            _CHATGPT_SESSION = cffi_requests.Session(impersonate=CHATGPT_IMPERSONATE)
        except Exception:
            return None
        return _CHATGPT_SESSION


_CHATGPT_SESSION = None
_SESSION_LOCK = threading.Lock()


def _headers(creds):
    """Session headers plus the current bearer token, when available."""
    hdrs = dict(creds.make_headers())
    auth = creds.auth_headers()
    if auth:
        hdrs.update(auth)
    return hdrs


def _post(creds, path, payload, headers=None, timeout=30, content_callback=None):
    """POST one chatgpt.com API call, refreshing the accessToken on 401.

    Returns the response, or None when the request could not be made at all.
    """
    hdrs = _headers(creds)
    if headers:
        hdrs.update(headers)

    def send(token_headers=None):
        h = dict(hdrs)
        h.update(token_headers or {})
        sess = _get_session()
        if sess is not None:
            return sess.post(
                f"{CHATGPT_URL_BASE}{path}",
                json=payload,
                headers=h,
                timeout=timeout,
                content_callback=content_callback,
            )
        return cffi_requests.post(
            f"{CHATGPT_URL_BASE}{path}",
            json=payload,
            headers=h,
            impersonate=CHATGPT_IMPERSONATE,
            content_callback=content_callback,
            timeout=timeout,
        )

    try:
        resp = send()
    except Exception as e:
        print(f"\n[!] ChatGPT request error ({path}): {e}")
        return None
    if resp is not None and resp.status_code == 401 and creds.refresh_token():
        try:
            return send(creds.auth_headers())
        except Exception as e:
            print(f"\n[!] ChatGPT request error ({path}): {e}")
            return None
    return resp


# ── Sentinel handshake ──────────────────────────────────────────────────────


def _turnstile_fallback():
    """Captured Turnstile token, kept out of the repo as session data."""
    token = os.environ.get("SI_CHATGPT_TURNSTILE", "").strip()
    if token:
        return token
    try:
        with open(os.path.join(_CONFIG_DIR, "chatgpt_turnstile.txt")) as f:
            return f.read().strip()
    except OSError:
        return ""


def acquire_sentinel_tokens(creds):
    """Run prepare + finalize and return the sentinel headers for a turn.

    Returns a dict ready to merge into a request's headers, or None (with the
    reason already printed) when the handshake fails.
    """
    p = encode_blob(build_fingerprint())
    resp = _post(creds, "/backend-api/sentinel/chat-requirements/prepare", {"p": p})
    if resp is None or resp.status_code != 200:
        code = resp.status_code if resp is not None else "no response"
        detail = ""
        try:
            detail = (resp.text or "")[:200]
        except Exception:
            pass
        print(f"\n[!] ChatGPT prepare failed (HTTP {code}): {detail}")
        return None
    try:
        prep = resp.json()
    except Exception:
        print("\n[!] ChatGPT prepare returned non-JSON.")
        return None

    pow_conf = prep.get("proofofwork") or {}
    answer = solve_pow(pow_conf.get("seed"), pow_conf.get("difficulty"))
    if answer is None:
        print("\n[!] Could not solve the ChatGPT proof-of-work challenge.")
        return None

    turnstile = (prep.get("turnstile") or {}).get("token") or _turnstile_fallback()
    if not turnstile:
        print(
            "\n[!] ChatGPT sentinel returned no Turnstile token; "
            "set SI_CHATGPT_TURNSTILE or re-capture one."
        )
        return None
    fin = {
        "prepare_token": prep.get("prepare_token"),
        "proofofwork": answer,
        "turnstile": turnstile,
    }
    resp = _post(creds, "/backend-api/sentinel/chat-requirements/finalize", fin)
    if resp is None or resp.status_code != 200:
        code = resp.status_code if resp is not None else "no response"
        detail = ""
        try:
            detail = (resp.text or "")[:200]
        except Exception:
            pass
        print(f"\n[!] ChatGPT finalize failed (HTTP {code}): {detail}")
        return None
    try:
        token = resp.json().get("token")
    except Exception:
        token = None
    if not token:
        print("\n[!] ChatGPT finalize returned no requirements token.")
        return None

    return {
        "openai-sentinel-chat-requirements-token": token,
        "openai-sentinel-proof-token": answer,
        "openai-sentinel-turnstile-token": turnstile,
    }


# ── Conversation ────────────────────────────────────────────────────────────


def create_conversation(creds):
    """Start a fresh conversation server-side and return its id.

    The webview posts a null conversation_id and lets the server mint one, so
    the client-side uuid is only a fallback for responses without an id.
    """
    resp = _post(
        creds,
        "/backend-api/conversation/init",
        {
            "conversation_id": None,
            "conversation_origin": None,
            "gizmo_id": None,
            "requested_default_model": None,
            "system_hints": None,
            "timezone": detect_timezone(),
            "timezone_offset_min": 0,
        },
    )
    if resp is None or resp.status_code != 200:
        code = resp.status_code if resp is not None else "no response"
        print(f"\n[!] Could not start a ChatGPT conversation (HTTP {code}).")
        return None
    try:
        return resp.json().get("conversation_id") or str(uuid.uuid4())
    except Exception:
        return str(uuid.uuid4())


def delete_conversation(creds, conv_id):
    """Delete a conversation so it never appears in the browser sidebar.

    Goes through the chatgpt-pinned session (not http.request) so the TLS
    fingerprint matches the chatgpt.com headers the edge checks see.
    """
    url = f"{CHATGPT_URL_BASE}/backend-api/conversation/{conv_id}"
    hdrs = _headers(creds)
    try:
        sess = _get_session()
        if sess is not None:
            sess.delete(url, headers=hdrs, timeout=15)
        else:
            cffi_requests.delete(url, headers=hdrs, impersonate=CHATGPT_IMPERSONATE, timeout=15)
    except Exception:
        pass  # best-effort cleanup


def _conversation_payload(prompt, model, conv_id, parent_id):
    """Build the /backend-api/f/conversation body.

    The payload mirrors what the Codex webview posts, minus its UI-only fields
    (genui snapshots, prepare state) which the endpoint tolerates being absent.
    """
    now = time.time()
    return {
        "turn_attribution": {"turn_trigger": "composer"},
        "action": "next",
        "is_do_not_remember": False,
        "model": model or CHATGPT_DEFAULT_MODEL,
        "parent_message_id": parent_id or "client-created-root",
        "timezone": detect_timezone(),
        "timezone_offset_min": 0,
        "client_contextual_info": {
            "app_name": "chatgpt.com",
            "app_surface": "codex_browser",
            "has_web_push_capabilities": False,
            "web_push_notification_permission": "default",
        },
        "local_function_names": [],
        "supported_encodings": ["v1"],
        "genui_state_snapshots": [],
        "client_prepare_state": "sent",
        "messages": [
            {
                "id": str(uuid.uuid4()),
                "author": {"metadata": {}, "name": None, "role": "user"},
                "creation_mode": None,
                "content": {"content_type": "text", "parts": [prompt], "text": prompt},
                "create_time": now,
                "metadata": {"serialization_metadata": {"custom_symbol_offsets": {}}},
                "recipient": "all",
                "status": "finished_successfully",
                "end_turn": None,
                "weight": 0.0,
                "channel": None,
                "complete_at": None,
            }
        ],
        "_conversation_id": conv_id,
    }


def _apply_patch(ops, parts, emit, state):
    """Fold a JSON-patch op list into the answer.

    Later turns append text as patch ops rather than resending snapshots, so
    both shapes must be folded. Status ops only record state: the patch can
    precede the last text op, so ending the turn here would truncate.
    """
    ended = False
    for op in ops:
        if not isinstance(op, dict):
            continue
        path, kind = op.get("p"), op.get("o")
        if path == "/message/status":
            state["status"] = op.get("v")
        elif path == "/message/content/parts/0" and kind == "append":
            delta = op.get("v")
            if isinstance(delta, str) and delta:
                parts.append(delta)
                emit(delta)
        elif path == "/message/content/parts/0" and kind == "replace":
            # Wholesale rewrite: re-emit the whole replacement.
            delta = op.get("v")
            if isinstance(delta, str):
                del parts[:]
                parts.append(delta)
                emit(delta)
        elif kind == "patch" and isinstance(op.get("v"), list):
            ended = _apply_patch(op["v"], parts, emit, state) or ended
    return ended


def _consume_event(event, parts, emit, state):
    """Fold one SSE event into the answer.

    Only assistant text snapshots, an embedded error and the terminal status
    matter; markers, titles and metadata are server bookkeeping. Returns True
    when the turn is over, which must happen only at a message boundary —
    returning from the curl content callback aborts the read.

    Text arrives as full snapshots rather than token deltas, so the new tail is
    emitted by length and earlier text is replaced wholesale when corrected.
    """
    lines = event.split("\n")
    data = next((ln[6:] for ln in lines if ln.startswith("data: ")), None)
    if data is None:
        return False
    if not data.strip():
        return False  # empty data lines are heartbeats, not end-of-stream
    if data.strip() == "[DONE]":
        return True
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return False
    if isinstance(obj, list):
        return _apply_patch(obj, parts, emit, state)
    if not isinstance(obj, dict):
        return False

    v = obj.get("v")
    if isinstance(v, list):
        # Patch list inside a delta envelope ({"o":"patch","v":[...]}).
        return _apply_patch(v, parts, emit, state)
    if isinstance(v, dict):
        if v.get("error") or v.get("error_code"):
            # Refusals and quota hits arrive as an in-band error message.
            err = v.get("error")
            if isinstance(err, dict):
                err = err.get("message") or json.dumps(err)
            state["error"] = str(err or v.get("error_code"))
            return True
        msg = v.get("message")
        if isinstance(msg, dict):
            role = (msg.get("author") or {}).get("role")
            status = msg.get("status")
            # User/system snapshots carry their own text (and a spurious
            # finished_successfully), so only assistant messages are folded in.
            if role != "assistant":
                return False
            if status and status != "in_progress":
                state["status"] = status
                # Deliberately not ending the turn here: assistant snapshots
                # can carry a terminal status while more text is still
                # streaming, and ending early truncates the answer. The
                # stream's own EOF ends consumption.
            content = msg.get("content") or {}
            text = content.get("text")
            if text is None and isinstance(content.get("parts"), list):
                text = "".join(p for p in content["parts"] if isinstance(p, str))
            if text and content.get("content_type") == "text":
                if parts and not text.startswith("".join(parts)):
                    del parts[:]  # server rewrote the answer; start from it
                delta = text[len("".join(parts)) :] if parts else text
                if delta:
                    parts.append(delta)
                    emit(delta)
        return False
    if obj.get("type") == "message_stream_complete":
        return True
    return False


def stream_prompt(
    creds,
    prompt,
    model=CHATGPT_DEFAULT_MODEL,
    stealth=True,
    session_state=None,
    on_text=None,
):
    """Send a prompt to ChatGPT and return the reply text.

    Mirrors streaming.stream_prompt's contract: one conversation per session
    and None (never "") on failure so callers cannot mistake an error for a
    blank answer. Deletion is the caller's job (see cli.py); ``stealth`` is
    accepted for signature parity and currently unused.
    """
    if session_state is None:
        session_state = {}

    def emit(txt):
        if on_text:
            on_text(txt)
        else:
            sys.stdout.write(txt)
            sys.stdout.flush()

    if not session_state.get("conv_id"):
        conv_id = create_conversation(creds)
        if not conv_id:
            return None
        session_state["conv_id"] = conv_id
        session_state["created"] = True
        session_state["parent_id"] = "client-created-root"

    sentinel = acquire_sentinel_tokens(creds)
    if sentinel is None:
        return None

    payload = _conversation_payload(
        prompt, model, session_state["conv_id"], session_state.get("parent_id")
    )
    parent_id = payload["messages"][0]["id"]

    state = {}
    parts = []
    # Buffered as bytes: a multi-byte character split across two network chunks
    # must be joined before decoding or it becomes two replacement characters.
    tail = [b""]

    def on_chunk(chunk):
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", "replace")
        tail[0] += chunk
        text = tail[0].decode("utf-8", "replace")
        while "\n\n" in text:
            event, text = text.split("\n\n", 1)
            tail[0] = text.encode("utf-8", "replace")
            if _consume_event(event, parts, emit, state):
                state["done"] = True
                tail[0] = b""  # turn over; drop anything that follows
                return

    resp = _post(
        creds,
        "/backend-api/f/conversation",
        payload,
        headers={**sentinel, "accept": "text/event-stream"},
        timeout=300,
        content_callback=on_chunk,
    )

    if resp is None:
        print("\n[!] ChatGPT request failed before completion.")
        return None
    if resp.status_code != 200:
        status = resp.status_code
        try:
            body = (resp.text or "")[:300]
        except Exception:
            body = ""
        if status in (401, 403):
            print("\n[!] ChatGPT session invalid or expired. Re-run --chatgpt --from-chrome")
        elif status == 429:
            print("\n[!] ChatGPT rate limited. Try again shortly.")
        else:
            print(f"\n[!] ChatGPT error (HTTP {status}): {body}")
        return None

    if state.get("error"):
        print(f"\n[!] ChatGPT refused the turn: {state['error']}")
        return None
    # A terminal event is the normal end. A terminal successful snapshot also
    # counts after EOF: some surfaces close without message_stream_complete.
    if not state.get("done") and state.get("status") != "finished_successfully":
        status = state.get("status")
        if status and status != "in_progress":
            print(f"\n[!] ChatGPT turn ended as {status}")
        else:
            print("\n[!] ChatGPT stream ended before completion; discarding the partial reply.")
        return None

    full = "".join(parts)
    if not full:
        if state.get("status") and state["status"] != "finished_successfully":
            print(f"\n[!] ChatGPT turn ended as {state['status']}")
        else:
            print("\n[!] ChatGPT returned no text.")
        return None

    if on_text is None:
        print()

    # Keep the thread so the next turn continues this conversation.
    session_state["parent_id"] = parent_id

    path = os.path.join(_CONFIG_DIR, "last_response.txt")
    tmp = f"{path}.{uuid.uuid4().hex[:8]}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(full)
    os.replace(tmp, path)
    return full
