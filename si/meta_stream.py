"""Meta AI streaming backend (GraphQL warmup + protobuf WebSocket).

Wire format pinned against Meta's current Ecto-era web client:

    POST /api/graphql  {doc_id: warmup, variables: {conversationId}}
    POST /api/graphql  {doc_id: mode,   variables: {input: {conversationId, mode}}}
    WSS  gateway.meta.ai/ws/clippy?Authorization=ecto1:...

The WebSocket carries an intro frame followed by a protobuf prompt frame built
from a captured template. Replies arrive as JSON objects inside binary frames:
`type:"full"` snapshots and `type:"patch"` text deltas.

Like the chatgpt backend, one server-side conversation backs a whole session.
Meta exposes no delete mutation, so cleanup is a no-op.
"""

import base64
import json
import os
import queue
import sys
import threading
import time
import uuid
from urllib.parse import urlencode

from .compat import cffi_requests
from .config import (
    _CONFIG_DIR,
    META_DEFAULT_MODEL,
    META_IMPERSONATE,
    META_MODEL_MODES,
    META_UA,
    META_URL_BASE,
)

GRAPHQL_API = f"{META_URL_BASE}/api/graphql"
META_WS_URL = "wss://gateway.meta.ai/ws/clippy"
META_WS_APP_ID = "1522763855472543"
META_WS_APP_VERSION = "1.0.0"
META_WS_AUTHTYPE = "15:0"
META_WS_DGW_VERSION = "5"
META_WS_DGW_UUID = "0"
META_WS_TIER = "prod"
META_WS_INTRO_FRAME_TYPE = 0x0F
META_WS_PROMPT_FRAME_TYPE = 0x0D
META_WS_PROMPT_FRAME_FLAG = 0x80

META_WARMUP_DOC_ID = "e7f802582dbfed8e181b012e010993eb"
META_MODE_SWITCH_DOC_ID = "c32bbe999c48e64e855dc63177d5153f"

BASE62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
WS_TIMEOUT_SECONDS = 120

# Captured protobuf templates from Meta's web client (2026-09-05 capture).
# Only ids, timestamps and prompt text are mutated per turn.
META_WS_HOME_TEMPLATE_B64 = "CsAGCswDCiBLQURBQlJBX19IT01FX19VTklGSUVEX0lOUFVUX0JBUhIQMTUyMjc2Mzg1NTQ3MjU0MyInNWE1Yi04ZDRlLWYwNTQtOTllZi1iMmRlLWRiMDItMGQwNS01MmM3KigqJgokNWIxMzk4YmEtZDdmYi00ZjczLWI5MTYtY2JhMzE4ODBjODVmMAU6C0hVTUFOX0FHRU5UQiIKDzg2NzA1MTMxNDc2NzY5NhIPODY3MDUxMzE0NzY3Njk2UgVFQ1RPMVoRQWJyYSBXZWIgTWFpbiBLZXliGBoSCOkHEg1tb2RlX3RoaW5raW5nIgIIAWoHV2luZG93c3IKdXNlcl9pbnB1dHpvTW96aWxsYS81LjAgKFdpbmRvd3MgTlQgMTAuMDsgV2luNjQ7IHg2NCkgQXBwbGVXZWJLaXQvNTM3LjM2IChLSFRNTCwgbGlrZSBHZWNrbykgQ2hyb21lLzE1Mi4wLjAuMCBTYWZhcmkvNTM3LjM2ggELZGVza3RvcF93ZWKaAUcKQDNlNjRlN2IwYzI4MmZiNTY3NzI0ODIxNTljZjAzMTcxNjkxYWQxYjM0ODBkNjk5M2E4NDJiMDQwMjIxZTM4YzEVAACAPxIUCPSTqtzR+oYCEPSTqtzR+oYCGAIaAiABIgAqDgip9tKHhzQY5/XSh4c0MiQ3NWExMDhlZS1hZjFiLTQyZTUtOTFhMi1jYjVkZmNhOTEwOTA6BGABaAFKBxIFemgtQ05ScgokZTc4ZWFhZjUtZTY3MC00MjczLWE1NjktZjgwMTkyNDc4MTNhGiRjZjQ4N2QyNi05MDBhLTQ3ZjYtODhjMS1iMmNkMGEwNjM4NWQiJDViMTM5OGJhLWQ3ZmItNGY3My1iOTE2LWNiYTMxODgwYzg1ZnoMIgpBc2lhL1Rva3lvggEDsAEBkgEMCgZzdG9ja3MSAggBkgENCgd3ZWF0aGVyEgIIAZIBJAoebWV0YV9rbm93bGVkZ2Vfc2VhcmNoX2Nhcm91c2VsEgIIAZIBIgocbWV0YV9jYXRhbG9nX3NlYXJjaF9jYXJvdXNlbBICCAGSARMKDW1lZGlhX2dhbGxlcnkSAggBogEBA9ABABJsCmEKJDM4NzlmMDJlLWZkNDUtNGJjNS04YjgyLTlhNDkxMDFkYjRjNhI3CiQ1YjEzOThiYS1kN2ZiLTRmNzMtYjkxNi1jYmEzMTg4MGM4NWYQqvbSh4c0GJ3ApOrrpY+OaCgBEgJIaSIDCgEw"
META_WS_CHAT_TEMPLATE_B64 = "CsAGCswDCiBLQURBQlJBX19DSEFUX19VTklGSUVEX0lOUFVUX0JBUhIQMTUyMjc2Mzg1NTQ3MjU0MyInNWE1Yi04ZDRlLWYwNTQtOTllZi1iMmRlLWRiMDItMGQwNS01MmM3KigqJgokNWIxMzk4YmEtZDdmYi00ZjczLWI5MTYtY2JhMzE4ODBjODVmMAU6C0hVTUFOX0FHRU5UQiIKDzg2NzA1MTMxNDc2NzY5NhIPODY3MDUxMzE0NzY3Njk2UgVFQ1RPMVoRQWJyYSBXZWIgTWFpbiBLZXliGBoSCOkHEg1tb2RlX3RoaW5raW5nIgIIAWoHV2luZG93c3IKdXNlcl9pbnB1dHpvTW96aWxsYS81LjAgKFdpbmRvd3MgTlQgMTAuMDsgV2luNjQ7IHg2NCkgQXBwbGVXZWJLaXQvNTM3LjM2IChLSFRNTCwgbGlrZSBHZWNrbykgQ2hyb21lLzE1Mi4wLjAuMCBTYWZhcmkvNTM3LjM2ggELZGVza3RvcF93ZWKaAUcKQDNlNjRlN2IwYzI4MmZiNTY3NzI0ODIxNTljZjAzMTcxNjkxYWQxYjM0ODBkNjk5M2E4NDJiMDQwMjIxZTM4YzEVAACAPxIUCPSTqtzR+oYCEPSTqtzR+oYCGAIaAiABIgAqDgip9tKHhzQY5/XSh4c0MiQ3NWExMDhlZS1hZjFiLTQyZTUtOTFhMi1jYjVkZmNhOTEwOTA6BGABaAFKBxIFemgtQ05ScgokZTc4ZWFhZjUtZTY3MC00MjczLWE1NjktZjgwMTkyNDc4MTNhGiRjZjQ4N2QyNi05MDBhLTQ3ZjYtODhjMS1iMmNkMGEwNjM4NWQiJDViMTM5OGJhLWQ3ZmItNGY3My1iOTE2LWNiYTMxODgwYzg1ZnoMIgpBc2lhL1Rva3lvggEDsAEBkgEMCgZzdG9ja3MSAggBkgENCgd3ZWF0aGVyEgIIAZIBJAoebWV0YV9rbm93bGVkZ2Vfc2VhcmNoX2Nhcm91c2VsEgIIAZIBIgocbWV0YV9jYXRhbG9nX3NlYXJjaF9jYXJvdXNlbBICCAGSARMKDW1lZGlhX2dhbGxlcnkSAggBogEBA9ABABJsCmEKJDM4NzlmMDJlLWZkNDUtNGJjNS04YjgyLTlhNDkxMDFkYjRjNhI3CiQ1YjEzOThiYS1kN2ZiLTRmNzMtYjkxNi1jYmEzMTg4MGM4NWYQqvbSh4c0GJ3ApOrrpY+OaCgBEgJIaSIDCgEw"


# ── ids ─────────────────────────────────────────────────────────────────────


def _encode_base62(value, pad_length):
    encoded = ""
    while value > 0:
        encoded = BASE62_ALPHABET[value % 62] + encoded
        value //= 62
    return encoded.rjust(pad_length, "0")


def generate_conversation_id():
    packed = ((int(time.time() * 1000) & ((1 << 44) - 1)) << 64) | int.from_bytes(
        os.urandom(8), "big"
    )
    return "c." + _encode_base62(packed, 19)


def _numeric_message_id(submitted_ms):
    return submitted_ms * 10000 + int.from_bytes(os.urandom(2), "big") % 10000


# ── protobuf helpers ────────────────────────────────────────────────────────


def _decode_varint(data, offset):
    value = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("truncated varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _encode_varint(value):
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def parse_proto_fields(data):
    """Return a list of {number, wireType, value} dicts."""
    fields = []
    offset = 0
    while offset < len(data):
        tag, offset = _decode_varint(data, offset)
        number, wire_type = tag >> 3, tag & 0x07
        if wire_type == 0:
            value, offset = _decode_varint(data, offset)
        elif wire_type == 1:
            value = int.from_bytes(data[offset : offset + 8], "little")
            offset += 8
        elif wire_type == 2:
            length, offset = _decode_varint(data, offset)
            value = data[offset : offset + length]
            offset += length
        elif wire_type == 5:
            value = int.from_bytes(data[offset : offset + 4], "little")
            offset += 4
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
        fields.append({"number": number, "wireType": wire_type, "value": value})
    return fields


def serialize_proto_fields(fields):
    parts = []
    for field in fields:
        parts.append(_encode_varint((field["number"] << 3) | field["wireType"]))
        wire_type, value = field["wireType"], field["value"]
        if wire_type == 0:
            parts.append(_encode_varint(value))
        elif wire_type == 1:
            parts.append(int(value).to_bytes(8, "little"))
        elif wire_type == 2:
            raw = value if isinstance(value, bytes) else str(value).encode()
            parts.append(_encode_varint(len(raw)))
            parts.append(raw)
        elif wire_type == 5:
            parts.append(int(value).to_bytes(4, "little"))
    return b"".join(parts)


def _find_field(fields, number):
    return next((f for f in fields if f["number"] == number), None)


def _traverse_and_mutate(fields, path, mutator):
    if not path:
        return False
    field = _find_field(fields, path[0])
    if field is None or not isinstance(field["value"], bytes):
        return False
    if len(path) == 1:
        mutator(field)
        return True
    nested = parse_proto_fields(field["value"])
    if _traverse_and_mutate(nested, path[1:], mutator):
        field["value"] = serialize_proto_fields(nested)
        return True
    return False


def _set_text(field, text):
    field["value"] = text.encode()


def _set_int(field, value):
    field["value"] = value


# ── WebSocket frames ────────────────────────────────────────────────────────


def _header(frame_type, sub_session_idx, length):
    return bytes(
        [
            frame_type,
            sub_session_idx & 0xFF,
            (sub_session_idx >> 8) & 0xFF,
            length & 0xFF,
            (length >> 8) & 0xFF,
            (length >> 16) & 0xFF,
        ]
    )


def build_ws_intro_frame(conversation_id):
    payload = json.dumps(
        {
            "x-dgw-app-x-ecto-conversation-id": conversation_id,
            "x-dgw-app-client-payload-type": "PROTO_INSIDE_JSON",
        },
        separators=(",", ":"),
    ).encode()
    return _header(META_WS_INTRO_FRAME_TYPE, 0, len(payload)) + payload


def build_ws_prompt_frame(prompt, conversation_id, template_b64, request_id=None):
    request_id = request_id or str(uuid.uuid4())
    user_message_id = str(uuid.uuid4())
    submitted_ms = int(time.time() * 1000)
    unique_message_id = _numeric_message_id(submitted_ms)

    fields = parse_proto_fields(base64.b64decode(template_b64))

    _traverse_and_mutate(fields, [1, 1, 5, 5, 1], lambda f: _set_text(f, conversation_id))

    def mutate_user_message(field):
        nested = parse_proto_fields(field["value"])
        target = _find_field(nested, 1)
        if target:
            _set_text(target, user_message_id)
        field["value"] = serialize_proto_fields(nested)

    _traverse_and_mutate(fields, [2, 1], mutate_user_message)

    def mutate_conv_timestamps(field):
        nested = parse_proto_fields(field["value"])
        for number, value in (
            (1, conversation_id),
            (2, submitted_ms),
            (3, unique_message_id),
        ):
            target = _find_field(nested, number)
            if target:
                if number == 1:
                    _set_text(target, value)
                else:
                    _set_int(target, value)
        field["value"] = serialize_proto_fields(nested)

    _traverse_and_mutate(fields, [2, 1, 2], mutate_conv_timestamps)

    def mutate_prompt(field):
        nested = parse_proto_fields(field["value"])
        target = _find_field(nested, 2)
        if target:
            _set_text(target, prompt)
        field["value"] = serialize_proto_fields(nested)

    _traverse_and_mutate(fields, [2], mutate_prompt)

    def mutate_timestamps(field):
        nested = parse_proto_fields(field["value"])
        first, third = _find_field(nested, 1), _find_field(nested, 3)
        if first:
            _set_int(first, submitted_ms + 1)
        if third:
            _set_int(third, submitted_ms)
        field["value"] = serialize_proto_fields(nested)

    _traverse_and_mutate(fields, [1, 5], mutate_timestamps)

    def mutate_request(field):
        nested = parse_proto_fields(field["value"])
        target = _find_field(nested, 6)
        if target:
            _set_text(target, request_id)
        field["value"] = serialize_proto_fields(nested)

    _traverse_and_mutate(fields, [1], mutate_request)

    def mutate_conv_id(field):
        nested = parse_proto_fields(field["value"])
        target = _find_field(nested, 4)
        if target:
            _set_text(target, conversation_id)
        field["value"] = serialize_proto_fields(nested)

    _traverse_and_mutate(fields, [1, 10], mutate_conv_id)

    payload_b64 = base64.b64encode(serialize_proto_fields(fields)).decode()
    inner = json.dumps(
        {"req-id": request_id, "payload": payload_b64}, separators=(",", ":")
    ).encode()
    body = bytes([0, META_WS_PROMPT_FRAME_FLAG]) + inner
    return _header(META_WS_PROMPT_FRAME_TYPE, 0, len(body)) + body


def build_ws_url(authorization, request_id):
    params = {
        "x-dgw-appid": META_WS_APP_ID,
        "x-dgw-appversion": META_WS_APP_VERSION,
        "x-dgw-authtype": META_WS_AUTHTYPE,
        "x-dgw-version": META_WS_DGW_VERSION,
        "x-dgw-uuid": META_WS_DGW_UUID,
        "x-dgw-tier": META_WS_TIER,
        "Authorization": authorization,
        "x-dgw-app-origin": "meta.ai",
        "x-dgw-app-clippy-request-id": request_id,
        "x-dgw-app-clippy-async": "true",
    }
    return f"{META_WS_URL}?{urlencode(params)}"


def parse_ws_response_events(payload):
    """Extract top-level JSON objects from a WS message (frames may mix text)."""
    events = []
    start = None
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(payload):
        if start is None:
            if ch == "{":
                start, depth, in_string, escape = i, 1, False, False
            continue
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    events.append(json.loads(payload[start : i + 1]))
                except ValueError:
                    pass
                start = None
    return events


# ── GraphQL handshake ───────────────────────────────────────────────────────


def _graphql_post(creds, doc_id, variables):
    headers = creds.make_headers("multipart/mixed, application/json")
    try:
        resp = cffi_requests.post(
            GRAPHQL_API,
            json={"doc_id": doc_id, "variables": variables},
            headers=headers,
            impersonate=META_IMPERSONATE,
            timeout=30,
        )
    except Exception as e:
        print(f"\n[!] Meta GraphQL error: {e}")
        return False
    if resp is None or resp.status_code != 200:
        code = resp.status_code if resp is not None else "no response"
        print(f"\n[!] Meta GraphQL failed (HTTP {code}).")
        return False
    try:
        data = resp.json()
    except Exception:
        return True
    if isinstance(data, dict) and data.get("errors"):
        errors = data["errors"] or []
        message = (
            (errors[0] or {}).get("message") if isinstance(errors[0], dict) else str(errors[0])
        )
        print(f"\n[!] Meta GraphQL failed: {message}")
        return False
    return True


# ── WebSocket round ─────────────────────────────────────────────────────────


def _ws_round(url, headers, intro_frame, prompt_frame, deadline, on_message):
    """Send both frames and feed each received message to ``on_message``.

    Returns (error_text, timed_out). ``error_text`` is empty on a normal close.
    """
    try:
        session = cffi_requests.Session(impersonate=META_IMPERSONATE)
        ws = session.ws_connect(url, headers=headers, timeout=30)
    except Exception as e:
        return str(e), False

    try:
        ws.send(intro_frame)
        ws.send(prompt_frame)
    except Exception as e:
        try:
            ws.close()
        except Exception:
            pass
        return str(e), False

    messages = queue.Queue()

    def pump():
        try:
            while True:
                data, _flags = ws.recv()
                messages.put(data)
        except Exception as e:  # normal close raises too
            messages.put(e)

    threading.Thread(target=pump, daemon=True).start()

    error = ""
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "", True
            try:
                item = messages.get(timeout=remaining)
            except queue.Empty:
                return "", True
            if isinstance(item, BaseException):
                error = "" if "closed" in type(item).__name__.lower() else str(item)
                return error, False
            on_message(item)
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _event_text(events, accumulated):
    """Fold response events into (new_text, new_accumulated)."""
    text = accumulated
    chunks = []
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "full":
            for section in (event.get("response") or {}).get("sections") or []:
                value = (((section or {}).get("view_model") or {}).get("primitive") or {}).get(
                    "text"
                )
                if value and value != text:
                    delta = value.removeprefix(text)
                    if delta:
                        chunks.append(delta)
                    text = value
        elif kind == "patch":
            for op in event.get("operations") or []:
                if (
                    isinstance(op, dict)
                    and op.get("op") == "delta"
                    and op.get("path") == "/sections/0/view_model/primitive/text"
                    and isinstance(op.get("value"), str)
                ):
                    chunks.append(op["value"])
                    text += op["value"]
    return "".join(chunks), text


def stream_prompt(
    creds,
    prompt,
    model=META_DEFAULT_MODEL,
    stealth=True,
    session_state=None,
    on_text=None,
):
    """Send a prompt to Meta AI and return the reply text (None on failure).

    Mirrors stream_prompt's contract: one conversation per session and None
    (never "") on failure. ``stealth`` is accepted for signature parity and
    unused — Meta exposes no delete mutation.
    """
    if session_state is None:
        session_state = {}

    def emit(text):
        if text:
            if on_text:
                on_text(text)
            else:
                sys.stdout.write(text)
                sys.stdout.flush()

    new_conversation = not session_state.get("conv_id")
    if new_conversation:
        session_state["conv_id"] = generate_conversation_id()
        session_state["created"] = True
    conv_id = session_state["conv_id"]

    authorization = creds.auth_token()
    if not authorization:
        return None

    mode = META_MODEL_MODES.get(model, META_MODEL_MODES[META_DEFAULT_MODEL])
    if not _graphql_post(creds, META_WARMUP_DOC_ID, {"conversationId": conv_id}):
        _reset_if_new(session_state, new_conversation)
        return None
    if not _graphql_post(
        creds, META_MODE_SWITCH_DOC_ID, {"input": {"conversationId": conv_id, "mode": mode}}
    ):
        _reset_if_new(session_state, new_conversation)
        return None

    request_id = str(uuid.uuid4())
    template = META_WS_HOME_TEMPLATE_B64 if new_conversation else META_WS_CHAT_TEMPLATE_B64
    intro = build_ws_intro_frame(conv_id)
    frame = build_ws_prompt_frame(prompt, conv_id, template, request_id=request_id)
    headers = {
        "Cookie": creds.cookie_header(),
        "User-Agent": META_UA,
        "Origin": META_URL_BASE,
    }
    accumulated = ""
    deadline = time.monotonic() + WS_TIMEOUT_SECONDS

    def handle(data):
        nonlocal accumulated
        raw = data.decode("utf-8", "ignore") if isinstance(data, bytes) else data
        delta, accumulated = _event_text(parse_ws_response_events(raw), accumulated)
        emit(delta)

    error, timed_out = _ws_round(
        build_ws_url(authorization, request_id), headers, intro, frame, deadline, handle
    )
    if timed_out and not accumulated:
        print("\n[!] Meta AI WebSocket timed out.")
        _reset_if_new(session_state, new_conversation)
        return None
    if error and not accumulated:
        print(f"\n[!] Meta AI WebSocket error: {error}")
        _reset_if_new(session_state, new_conversation)
        return None
    if not accumulated:
        print("\n[!] Meta AI returned no assistant content.")
        _reset_if_new(session_state, new_conversation)
        return None

    if on_text is None:
        print()
    session_state["branch_path"] = "0"

    path = os.path.join(_CONFIG_DIR, "last_response.txt")
    tmp = f"{path}.{uuid.uuid4().hex[:8]}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(accumulated)
    os.replace(tmp, path)
    return accumulated


def _reset_if_new(session_state, new_conversation):
    """Drop a conversation that never started so the next turn retries."""
    if new_conversation:
        session_state["conv_id"] = None
        session_state["created"] = False


def delete_conversation(creds, conv_id):
    """Meta exposes no delete mutation: cleanup is deliberately a no-op."""
    return None
