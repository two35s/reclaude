"""OpenAI-compatible HTTP proxy adapter for reclaude.

Exposes a local server that accepts standard OpenAI /v1/chat/completions
requests and translates them into claude.ai internal API calls through
the existing http.py / streaming.py backend.

This allows any OpenAI-compatible client (Crush, archived opencode,
Continue.dev, etc.) to use your browser-session-authenticated Claude
backend without any modification to those clients.

Usage:
    python -m claude_re --proxy            # default port 8765
    python -m claude_re --proxy --port 9000

Set RECLAUDE_PROXY_TOKEN to require it as a bearer token
(Authorization: Bearer ...) on every request.

Then register in crushrc:
    provider add reclaude --type openai --base-url "http://localhost:8765/v1"
    model add reclaude/sonnet --name "Claude Sonnet (reclaude)" --context-window 200000

Or as LOCAL_ENDPOINT for the archived opencode binary:
    LOCAL_ENDPOINT=http://localhost:8765 opencode
"""

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import DEFAULT_MODEL, MODEL_ALIASES, resolve_model
from .streaming import stream_prompt
from .tool_calls import (
    ToolCallParser,
    parse_tool_calls,
    render_assistant_calls,
    render_tool_prompt,
)

# Proxy hardening: cap request bodies; optional bearer token via env.
MAX_BODY_BYTES = 1_000_000
PROXY_TOKEN = os.environ.get("RECLAUDE_PROXY_TOKEN", "")

# ── Payload translation ────────────────────────────────────────────────────


def _openai_messages_to_prompt(messages, tools=None, recent_only=False):
    """Convert OpenAI messages array to a plain text prompt.

    We reconstruct a multi-turn context string.  System prompts are
    prepended as a special block so the model can honour them.  The
    last user message becomes the active prompt; prior turns are
    embedded in it as a readable transcript so claude.ai's server-side
    conversation history still provides the real memory.

    ``recent_only`` drops turns before the last user message (system
    messages are always kept). Used when a persistent conversation id
    exists: the server-side conversation already holds earlier turns,
    so re-sending them would double the token cost every turn.

    Tool calls are emulated in text: ``tools`` become a spec + protocol
    block in the system section, an assistant turn carrying ``tool_calls``
    is replayed as the same fence syntax it was asked to emit, and ``tool``
    turns come back as ``[Tool result]`` so the model can continue.
    """
    parts = []
    system_parts = []

    start = 0
    if recent_only:
        start = next(
            (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"), 0
        )

    for i, msg in enumerate(messages):
        # Keep system instructions from any position; drop other early turns.
        if i < start and msg.get("role") != "system":
            continue
        role = msg.get("role", "")
        content = msg.get("content") or ""
        # content may be a list of content blocks (vision payloads, etc.)
        if isinstance(content, list):
            text_pieces = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            content = "\n".join(text_pieces)

        if role == "system":
            system_parts.append(content)
        elif role == "user":
            parts.append(f"[User]: {content}")
        elif role == "assistant":
            calls = msg.get("tool_calls") or []
            if calls:
                fence = render_assistant_calls(calls)
                content = f"{content}\n{fence}" if content else fence
            parts.append(f"[Assistant]: {content}")
        elif role == "tool":
            parts.append(f"[Tool result {msg.get('name') or 'tool'}]: {content}")

    tool_block = render_tool_prompt(tools)
    if tool_block:
        system_parts.append(tool_block)

    prompt = "\n\n".join(parts)
    if system_parts:
        system_block = "\n".join(system_parts)
        prompt = f"[System instructions]:\n{system_block}\n\n{prompt}"
    return prompt


def _resolve_model_from_openai_id(model_id):
    """Map an incoming OpenAI-style model id to a reclaude model id.

    Clients may send bare aliases ('sonnet', 'haiku'), full ids
    ('claude-sonnet-5-5'), or prefixed ids ('reclaude/sonnet',
    'reclaude/claude-sonnet-5-5').
    """
    model_id = model_id.removeprefix("reclaude/")
    return resolve_model(model_id)


# ── OpenAI SSE format helpers ──────────────────────────────────────────────


def _sse_chunk(content, model, finish_reason=None, tool_calls=None):
    """Format one SSE data chunk in OpenAI streaming format."""
    delta = {}
    if content is not None:
        delta["content"] = content
    if tool_calls:
        delta["tool_calls"] = tool_calls
    chunk = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:10]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(chunk)}\n\n".encode()


def _tool_call_chunks(tool_calls):
    """One OpenAI streaming delta per tool call, carrying id/name/arguments.

    OpenAI clients accumulate these by `index`; sending the full call in a
    single delta is valid and avoids inventing argument fragmentation.
    """
    chunks = []
    for i, call in enumerate(tool_calls):
        chunks.append(
            {
                "index": i,
                "id": call["id"],
                "type": "function",
                "function": call["function"],
            }
        )
    return chunks


def _non_stream_response(full_text, model, tool_calls=None):
    """Format a complete (non-streaming) OpenAI chat.completion response."""
    message = {"role": "assistant", "content": full_text}
    finish_reason = "stop"
    if tool_calls:
        message["tool_calls"] = tool_calls
        finish_reason = "tool_calls"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:10]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


# ── HTTP handler ───────────────────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible HTTP request handler."""

    # Injected by the server factory
    creds = None
    session_registry: dict[str, dict] = {}  # conversation_id → session_state dict
    registry_lock = threading.Lock()

    # ── routing ──────────────────────────────────────────────────────

    def do_GET(self):
        if not self._authorized():
            self._error(403, "Missing or invalid proxy token")
            return
        if self.path in ("/v1/models", "/v1/models/"):
            self._list_models()
        else:
            self._not_found()

    def do_POST(self):
        if not self._authorized():
            self._error(403, "Missing or invalid proxy token")
            return
        if self.path in ("/v1/chat/completions", "/v1/chat/completions/"):
            self._chat_completions()
        else:
            self._not_found()

    def _authorized(self):
        return not PROXY_TOKEN or self.headers.get("Authorization") == f"Bearer {PROXY_TOKEN}"

    # ── model list ───────────────────────────────────────────────────

    def _list_models(self):
        models = [
            {
                "id": f"reclaude/{alias}",
                "object": "model",
                "created": 0,
                "owned_by": "reclaude",
            }
            for alias in MODEL_ALIASES
        ]
        # Also expose the default model directly
        models.append(
            {
                "id": DEFAULT_MODEL,
                "object": "model",
                "created": 0,
                "owned_by": "reclaude",
            }
        )
        body = json.dumps({"object": "list", "data": models}).encode()
        self._send(200, "application/json", body)

    # ── chat/completions ─────────────────────────────────────────────

    def _chat_completions(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            self._error(400, "Invalid Content-Length")
            return
        if length > MAX_BODY_BYTES:
            self.rfile.read(MAX_BODY_BYTES + 1)  # bounded drain so the 413 is delivered
            self._error(413, "Request body too large")
            return
        raw = self.rfile.read(length)
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            self._error(400, "Invalid JSON body")
            return

        messages = req.get("messages", [])
        if not messages:
            self._error(400, "No messages provided")
            return

        model_id = req.get("model", DEFAULT_MODEL)
        model = _resolve_model_from_openai_id(model_id)
        stream = req.get("stream", False)
        tools = req.get("tools") or []

        # Each request may carry an optional conversation_id header so
        # multi-turn sessions share server-side context.  Clients that
        # don't send one get a per-request ephemeral context.
        conv_key = self.headers.get("X-Conversation-Id") or req.get("user")

        # Build flat prompt (tools are described in-text when present).
        # With a persistent conversation the server already remembers
        # earlier turns, so only the recent ones are re-sent.
        prompt = _openai_messages_to_prompt(messages, tools=tools, recent_only=bool(conv_key))

        if conv_key:
            with self.registry_lock:
                session_state = self.session_registry.setdefault(conv_key, {})
        else:
            session_state = {}

        if stream:
            self._stream_response(prompt, model, model_id, session_state, tools)
        else:
            self._sync_response(prompt, model, model_id, session_state, tools)

    def _stream_response(self, prompt, model, model_id, session_state, tools=None):
        """Stream the response as SSE in OpenAI format."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        # Opening delta (role announcement)
        try:
            opening = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:10]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model_id,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            self.wfile.write(f"data: {json.dumps(opening)}\n\n".encode())
            self.wfile.flush()
        except BrokenPipeError:
            return

        error_flag = [False]
        # Tool calls are parsed out of the text stream; only text outside a
        # fence is forwarded as content, so the client never sees the raw
        # fence syntax.
        parser = ToolCallParser() if tools else None

        def on_text(chunk):
            if parser is not None:
                chunk = parser.feed(chunk)
                if not chunk:
                    return
            try:
                self.wfile.write(_sse_chunk(chunk, model_id))
                self.wfile.flush()
            except (BrokenPipeError, OSError):
                error_flag[0] = True

        full = stream_prompt(
            self.creds,
            prompt,
            model=model,
            stealth=False,  # proxy mode — caller manages lifetime
            session_state=session_state,
            on_text=on_text,
            allow_fallback=False,  # the client (OpenCode) owns retries; never swap models silently
        )

        if full is None and not error_flag[0]:
            # Headers are already sent, so surface the failure in-band:
            # an empty 200 with finish_reason "stop" reads as a blank reply.
            try:
                self.wfile.write(
                    b'data: {"error": {"message": "upstream Claude error - see proxy stderr",'
                    b' "type": "proxy_error"}}\n\n'
                )
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, OSError):
                pass
            return

        if not error_flag[0]:
            try:
                tool_calls = None
                finish_reason = "stop"
                if parser is not None:
                    tail, tool_calls = parser.finish()
                    if tail:
                        self.wfile.write(_sse_chunk(tail, model_id))
                    if tool_calls:
                        self.wfile.write(
                            _sse_chunk(
                                None,
                                model_id,
                                tool_calls=_tool_call_chunks(tool_calls),
                            )
                        )
                        finish_reason = "tool_calls"
                self.wfile.write(_sse_chunk(None, model_id, finish_reason=finish_reason))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, OSError):
                pass

    def _sync_response(self, prompt, model, model_id, session_state, tools=None):
        """Collect the full response then return it as one JSON object."""
        full = stream_prompt(
            self.creds,
            prompt,
            model=model,
            stealth=False,
            session_state=session_state,
            on_text=lambda _t: None,  # don't echo the response to the proxy's stdout
            allow_fallback=False,
        )
        if full is None:
            self._error(502, "Upstream Claude API returned no response")
            return
        tool_calls = None
        if tools:
            full, tool_calls = parse_tool_calls(full)
        body = json.dumps(_non_stream_response(full, model_id, tool_calls)).encode()
        self._send(200, "application/json", body)

    # ── helpers ──────────────────────────────────────────────────────

    def _send(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, message):
        body = json.dumps({"error": {"message": message, "type": "proxy_error"}}).encode()
        self._send(code, "application/json", body)

    def _not_found(self):
        self._error(404, f"Not found: {self.path}")

    def log_message(self, fmt, *args):  # quieter default logging
        print(f"[proxy] {self.address_string()} {fmt % args}")


# ── Public API ────────────────────────────────────────────────────────────


def run_proxy(creds, port=8765):
    """Start the OpenAI-compatible proxy on the given port.

    Blocks until interrupted (Ctrl+C).
    Prints the registration commands for Crush and archived opencode.
    """
    # Inject shared state into the handler class
    _Handler.creds = creds
    _Handler.session_registry = {}

    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    # shortcut: concurrent first-turn requests on the same conversation id can
    # each create a server-side conversation (one gets orphaned in the sidebar).
    # Upgrade to per-key locks if that ever shows up in practice.

    print(f"\n[proxy] reclaude OpenAI-compatible proxy listening on http://localhost:{port}")
    print("\n  Register in Crush (~/.config/crush/crushrc):")
    print(f'    provider add reclaude --type openai --base-url "http://localhost:{port}/v1"')
    print('    model add reclaude/sonnet --name "Claude Sonnet (reclaude)" --context-window 200000')
    print('    model add reclaude/haiku --name "Claude Haiku (reclaude)" --context-window 200000')
    print("\n  Or for archived opencode binary:")
    print(f"    LOCAL_ENDPOINT=http://localhost:{port} opencode")
    print("\n  Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[proxy] Shutting down.")
    finally:
        server.server_close()
