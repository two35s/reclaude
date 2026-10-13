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

Then register in crushrc:
    provider add reclaude --type openai --base-url "http://localhost:8765/v1"
    model add reclaude/sonnet --name "Claude Sonnet (reclaude)" --context-window 200000

Or as LOCAL_ENDPOINT for the archived opencode binary:
    LOCAL_ENDPOINT=http://localhost:8765 opencode
"""

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

from .config import DEFAULT_MODEL, MODEL_ALIASES, resolve_model
from .streaming import stream_prompt


# ── Payload translation ────────────────────────────────────────────────────


def _openai_messages_to_prompt(messages):
    """Convert OpenAI messages array to a plain text prompt.

    We reconstruct a multi-turn context string.  System prompts are
    prepended as a special block so the model can honour them.  The
    last user message becomes the active prompt; prior turns are
    embedded in it as a readable transcript so claude.ai's server-side
    conversation history still provides the real memory.
    """
    parts = []
    system_parts = []

    for msg in messages:
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
            parts.append(f"[Assistant]: {content}")

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
    if model_id.startswith("reclaude/"):
        model_id = model_id[len("reclaude/"):]
    return resolve_model(model_id)


# ── OpenAI SSE format helpers ──────────────────────────────────────────────


def _sse_chunk(content, model, finish_reason=None):
    """Format one SSE data chunk in OpenAI streaming format."""
    chunk = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:10]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": (
                    {"content": content}
                    if content is not None
                    else {}
                ),
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(chunk)}\n\n".encode()


def _non_stream_response(full_text, model):
    """Format a complete (non-streaming) OpenAI chat.completion response."""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:10]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": full_text},
                "finish_reason": "stop",
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
    session_registry = {}  # conversation_id → session_state dict
    registry_lock = threading.Lock()

    # ── routing ──────────────────────────────────────────────────────

    def do_GET(self):
        if self.path in ("/v1/models", "/v1/models/"):
            self._list_models()
        else:
            self._not_found()

    def do_POST(self):
        if self.path in ("/v1/chat/completions", "/v1/chat/completions/"):
            self._chat_completions()
        else:
            self._not_found()

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
        models.append({
            "id": DEFAULT_MODEL,
            "object": "model",
            "created": 0,
            "owned_by": "reclaude",
        })
        body = json.dumps({"object": "list", "data": models}).encode()
        self._send(200, "application/json", body)

    # ── chat/completions ─────────────────────────────────────────────

    def _chat_completions(self):
        length = int(self.headers.get("Content-Length", 0))
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

        # Build flat prompt
        prompt = _openai_messages_to_prompt(messages)

        # Each request may carry an optional conversation_id header so
        # multi-turn sessions share server-side context.  Clients that
        # don't send one get a per-request ephemeral context.
        conv_key = self.headers.get("X-Conversation-Id") or req.get("user")
        if conv_key:
            with self.registry_lock:
                session_state = self.session_registry.setdefault(conv_key, {})
        else:
            session_state = {}

        if stream:
            self._stream_response(prompt, model, model_id, session_state)
        else:
            self._sync_response(prompt, model, model_id, session_state)

    def _stream_response(self, prompt, model, model_id, session_state):
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

        def on_text(chunk):
            try:
                self.wfile.write(_sse_chunk(chunk, model_id))
                self.wfile.flush()
            except (BrokenPipeError, OSError):
                error_flag[0] = True

        stream_prompt(
            self.creds,
            prompt,
            model=model,
            stealth=False,           # proxy mode — caller manages lifetime
            session_state=session_state,
            on_text=on_text,
        )

        if not error_flag[0]:
            try:
                self.wfile.write(_sse_chunk(None, model_id, finish_reason="stop"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, OSError):
                pass

    def _sync_response(self, prompt, model, model_id, session_state):
        """Collect the full response then return it as one JSON object."""
        full = stream_prompt(
            self.creds,
            prompt,
            model=model,
            stealth=False,
            session_state=session_state,
        )
        if full is None:
            self._error(502, "Upstream Claude API returned no response")
            return
        body = json.dumps(_non_stream_response(full, model_id)).encode()
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

    server = HTTPServer(("127.0.0.1", port), _Handler)

    print(f"\n[proxy] reclaude OpenAI-compatible proxy listening on http://localhost:{port}")
    print(f"\n  Register in Crush (~/.config/crush/crushrc):")
    print(f'    provider add reclaude --type openai --base-url "http://localhost:{port}/v1"')
    print(f'    model add reclaude/sonnet --name "Claude Sonnet (reclaude)" --context-window 200000')
    print(f'    model add reclaude/haiku --name "Claude Haiku (reclaude)" --context-window 200000')
    print(f"\n  Or for archived opencode binary:")
    print(f"    LOCAL_ENDPOINT=http://localhost:{port} opencode")
    print(f"\n  Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[proxy] Shutting down.")
    finally:
        server.server_close()
