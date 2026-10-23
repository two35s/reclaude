"""OpenAI-compatible HTTP proxy adapter for si.

Exposes a local server that accepts standard OpenAI /v1/chat/completions
requests and translates them into claude.ai internal API calls through
the existing http.py / streaming.py backend.

This allows any OpenAI-compatible client (Crush, archived opencode,
Continue.dev, etc.) to use your browser-session-authenticated Claude
backend without any modification to those clients.

Usage:
    python -m si --proxy            # default port 8765
    python -m si --proxy --port 9000

Set SI_PROXY_TOKEN to require it as a bearer token
(Authorization: Bearer ...) on every request.

Then register in crushrc:
    provider add si --type openai --base-url "http://localhost:8765/v1"
    model add si/sonnet --name "Claude Sonnet (si)" --context-window 200000

Or as LOCAL_ENDPOINT for the archived opencode binary:
    LOCAL_ENDPOINT=http://localhost:8765 opencode
"""

import hmac
import json
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import CHATGPT_MODELS, DEFAULT_MODEL, META_MODELS, MODEL_ALIASES, resolve_model
from .streaming import stream_prompt as claude_stream_prompt
from .tool_calls import (
    ToolCallParser,
    parse_tool_calls,
    render_assistant_calls,
    render_tool_prompt,
)

# Proxy hardening: cap request bodies; optional bearer token via env.
MAX_BODY_BYTES = 1_000_000
PROXY_TOKEN = os.environ.get("SI_PROXY_TOKEN", "")

# Simple in-process rate limiting (requests per window) to avoid hammering upstream
RATE_LIMIT_REQUESTS = int(os.environ.get("SI_PROXY_RATE_LIMIT", "60"))
RATE_LIMIT_WINDOW = int(os.environ.get("SI_PROXY_RATE_WINDOW", "60"))
_rate_buckets = defaultdict(lambda: deque())
_rate_lock = threading.Lock()

# How much of a chatgpt reply is held back while checking for the prose
# refusal ("I can't reach that workspace") that surface produces on some
# tool-bearing turns instead of a fence.  Inside this window nothing has
# reached the client, so the retry below stays invisible.
_HOLD_WINDOW_CHARS = 200
_REFUSAL_MARKERS = (
    "can't",
    "can’t",
    "cannot",
    "couldn't",
    "couldn’t",
    "unable",
    "inaccessible",
    "not available",
    "not accessible",
    "no access",
    "don't have",
    "don’t have",
    "doesn't have",
    "doesn’t have",
    "isn't exposed",
    "aren't exposed",
    "not exposed",
    "returned an error",
)


def _looks_like_tool_refusal(text):
    """True when a chatgpt reply opens by refusing to act instead of calling.

    Only ever checked on the unforwarded opening of a tools-bearing reply, so
    a false positive costs one extra upstream turn, never user-visible text.
    """
    head = text[:_HOLD_WINDOW_CHARS].lower()
    return any(marker in head for marker in _REFUSAL_MARKERS)


# ── Payload translation ────────────────────────────────────────────────────


def _openai_messages_to_prompt(messages, tools=None, recent_only=False, backend="claude"):
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
    block, an assistant turn carrying ``tool_calls`` is replayed as the
    same fence syntax it was asked to emit, and ``tool`` turns come back
    as ``[Tool result]`` so the model can continue. For the chatgpt
    backend the block leads the prompt (see ``_HARNESS_PREAMBLE``).
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

    tool_block = render_tool_prompt(tools, harness=backend == "chatgpt")
    transcript = "\n\n".join(parts)
    system_block = "\n".join(system_parts) if system_parts else ""

    if backend == "chatgpt" and tool_block:
        sections = [tool_block]
        if system_block:
            sections.append(f"[System instructions]:\n{system_block}")
        if transcript:
            sections.append(transcript)
        return "\n\n".join(sections)

    if tool_block:
        system_parts.append(tool_block)
    if system_parts:
        system_block = "\n".join(system_parts)
        prompt = f"[System instructions]:\n{system_block}"
        return f"{prompt}\n\n{transcript}" if transcript else prompt
    return transcript


def _resolve_model_from_openai_id(model_id):
    """Map an incoming OpenAI-style model id to a (backend, model) pair.

    The 'chatgpt/' prefix routes to the chatgpt.com backend and 'meta/' to
    meta.ai; everything else (bare aliases, full ids, 'si/...' ids) is
    claude.ai.  Strip the 'si/' prefix first so 'si/chatgpt/auto' still
    routes to ChatGPT.
    """
    model_id = model_id.removeprefix("si/")
    if model_id.startswith("chatgpt/"):
        return "chatgpt", model_id.removeprefix("chatgpt/")
    if model_id.startswith("meta/"):
        return "meta", model_id.removeprefix("meta/")
    if model_id in META_MODELS:
        return "meta", model_id
    return "claude", resolve_model(model_id)


def _delete_conversation(backend, creds, conv_id):
    """Best-effort removal of a server-side conversation."""
    if not conv_id:
        return
    if backend == "chatgpt":
        from . import chatgpt_stream

        chatgpt_stream.delete_conversation(creds, conv_id)
    elif backend == "meta":
        return  # Meta has no delete mutation; the conversation stays in the sidebar
    else:
        from . import http as http_mod

        http_mod.delete_conversation(creds, conv_id)


def _adopt_conversation(current, fresh, backend, creds):
    """Replace a refusing conversation with the retry's fresh one."""
    new_id = fresh.get("conv_id")
    if not new_id:
        return
    old_id = current.get("conv_id")
    current.clear()
    current.update(fresh)
    if old_id and old_id != new_id:
        _delete_conversation(backend, creds, old_id)


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
    chatgpt_creds = None  # optional second backend; None disables chatgpt/ models
    meta_creds = None  # optional third backend; None disables meta/ models
    session_registry: dict[tuple, dict] = {}  # (backend, conversation_id) → session_state dict
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
            if self._rate_limit_check():
                return
            self._chat_completions()
        else:
            self._not_found()

    def _rate_limit_check(self):
        if RATE_LIMIT_REQUESTS <= 0:
            return False
        client = self.client_address[0] if self.client_address else "local"
        now = time.time()
        with _rate_lock:
            q = _rate_buckets[client]
            while q and now - q[0] > RATE_LIMIT_WINDOW:
                q.popleft()
            if len(q) >= RATE_LIMIT_REQUESTS:
                retry_after = int(q[0] + RATE_LIMIT_WINDOW - now) + 1
                body = json.dumps(
                    {"error": {"message": "Too many requests", "type": "rate_limit_error"}}
                ).encode()
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", str(max(1, retry_after)))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return True
            q.append(now)
        return False

    def _authorized(self):
        # Bytes: compare_digest raises on non-ASCII str, and header bytes
        # decode as latin-1, so garbage headers must not crash the handler.
        return not PROXY_TOKEN or hmac.compare_digest(
            (self.headers.get("Authorization") or "").encode(),
            f"Bearer {PROXY_TOKEN}".encode(),
        )

    # ── model list ───────────────────────────────────────────────────

    def _list_models(self):
        models = [
            {
                "id": f"si/{alias}",
                "object": "model",
                "created": 0,
                "owned_by": "si",
            }
            for alias in MODEL_ALIASES
        ]
        # Also expose the default model directly
        models.append(
            {
                "id": DEFAULT_MODEL,
                "object": "model",
                "created": 0,
                "owned_by": "si",
            }
        )
        if self.chatgpt_creds is not None:
            models.extend(
                {
                    "id": f"chatgpt/{slug}",
                    "object": "model",
                    "created": 0,
                    "owned_by": "openai",
                }
                for slug in CHATGPT_MODELS
            )
        if self.meta_creds is not None:
            models.extend(
                {
                    "id": f"meta/{slug}",
                    "object": "model",
                    "created": 0,
                    "owned_by": "meta",
                }
                for slug in META_MODELS
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
            self.close_connection = True  # body left unread; keep-alive would desync
            self._error(413, "Request body too large")
            return
        raw = self.rfile.read(length)
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            self._error(400, "Invalid JSON body")
            return
        if not isinstance(req, dict):
            self._error(400, "JSON body must be an object")
            return

        messages = req.get("messages")
        if not isinstance(messages, list) or not messages:
            self._error(400, "messages must be a non-empty list")
            return
        if not all(isinstance(m, dict) for m in messages):
            self._error(400, "each message must be an object")
            return

        model_id = req.get("model", DEFAULT_MODEL)
        if not isinstance(model_id, str) or not model_id:
            self._error(400, "model must be a non-empty string")
            return
        backend, model = _resolve_model_from_openai_id(model_id)
        if backend == "chatgpt" and self.chatgpt_creds is None:
            self._error(
                400,
                "chatgpt backend not configured — run 'python -m si --chatgpt --from-chrome' first",
            )
            return
        if backend == "meta" and self.meta_creds is None:
            self._error(
                400,
                "meta backend not configured — run 'python -m si --meta --from-chrome' first",
            )
            return
        stream = req.get("stream", False)
        tools = req.get("tools") or []

        # Each request may carry an optional conversation_id header so
        # multi-turn sessions share server-side context.  Clients that
        # don't send one get a per-request context that is deleted afterwards.
        # (OpenAI's `user` is an end-user id, not a conversation id.)
        conv_key = self.headers.get("X-Conversation-Id")

        # Build flat prompt (tools are described in-text when present).
        # With a persistent conversation the server already remembers
        # earlier turns, so only the recent ones are re-sent.
        prompt = _openai_messages_to_prompt(
            messages, tools=tools, recent_only=bool(conv_key), backend=backend
        )

        if conv_key:
            with self.registry_lock:
                # Keyed per backend: a client reusing one conversation key
                # across si/ and chatgpt/ models must not inherit the
                # other backend's conversation id.
                key = (backend, conv_key)
                # Cap the registry so weeks of proxy uptime don't grow it
                # forever.  Evicts the oldest conversation (dicts keep
                # insertion order); the server-side conversation itself
                # stays until removed by hand in the sidebar.
                # shortcut: no TTL — only new keys can push an old one out.
                if key not in self.session_registry and len(self.session_registry) >= 256:
                    self.session_registry.pop(next(iter(self.session_registry)))
                session_state = self.session_registry.setdefault(key, {})
        else:
            session_state = {}

        try:
            if stream:
                self._stream_response(prompt, model, model_id, session_state, tools, backend)
            else:
                self._sync_response(prompt, model, model_id, session_state, tools, backend)
        finally:
            if not conv_key:  # one-shot request: don't leak its conversation
                if backend == "chatgpt":
                    creds = self.chatgpt_creds
                elif backend == "meta":
                    creds = self.meta_creds
                else:
                    creds = self.creds
                _delete_conversation(backend, creds, session_state.get("conv_id"))

    def _dispatch(self, backend):
        """Return (stream_fn, creds) for the chosen backend."""
        if backend == "chatgpt":
            from . import chatgpt_stream

            return chatgpt_stream.stream_prompt, self.chatgpt_creds
        if backend == "meta":
            from . import meta_stream

            return meta_stream.stream_prompt, self.meta_creds
        return claude_stream_prompt, self.creds

    def _stream_response(
        self, prompt, model, model_id, session_state, tools=None, backend="claude"
    ):
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
        stream_fn, backend_creds = self._dispatch(backend)
        kwargs = {"model": model, "stealth": False}
        if backend == "claude":
            kwargs["allow_fallback"] = False  # client owns retries; never swap models silently
        can_retry = backend == "chatgpt" and bool(tools)

        def forward(text, parser):
            # Tool calls are parsed out of the text stream; only text outside
            # a fence is forwarded as content, so the client never sees the
            # raw fence syntax.
            if parser is not None:
                text = parser.feed(text)
                if not text:
                    return
            try:
                self.wfile.write(_sse_chunk(text, model_id))
                self.wfile.flush()
            except (BrokenPipeError, OSError):
                error_flag[0] = True

        def run_attempt(state):
            """One upstream call; returns (full, parser, held_text, live)."""
            parser = ToolCallParser() if tools else None
            held, live = [], not can_retry  # without a retry, stream immediately

            def on_text(chunk):
                nonlocal live
                if live:
                    forward(chunk, parser)
                    return
                # Hold the opening back so a refusal that triggers the retry
                # never reaches the client; a fence or a long enough opening
                # means the model is acting normally.
                held.append(chunk)
                head = "".join(held)
                if _looks_like_tool_refusal(head):
                    return
                if "```" in head or len(head) >= _HOLD_WINDOW_CHARS:
                    live = True
                    forward(head, parser)
                    held.clear()

            full = stream_fn(backend_creds, prompt, session_state=state, on_text=on_text, **kwargs)
            return full, parser, held, live

        full, parser, held, live = run_attempt(session_state)
        if can_retry and not live and not error_flag[0] and _looks_like_tool_refusal("".join(held)):
            # The chatgpt surface sometimes answers a tool request in prose
            # ("I can't reach that workspace") instead of a fence.  Nothing
            # has reached the client yet, so retry once in a fresh
            # conversation; if that also refuses, its prose is shown.
            # Keep the retry's conversation so the next turn doesn't resume
            # the one that refused.
            # shortcut: one retry, no backoff.
            retry_state = {}
            full, parser, held, live = run_attempt(retry_state)
            _adopt_conversation(session_state, retry_state, backend, backend_creds)

        if full is None and not error_flag[0]:
            # Headers are already sent, so surface the failure in-band:
            # an empty 200 with finish_reason "stop" reads as a blank reply.
            # shortcut: {"error": ...} is not standard OpenAI stream shape;
            # strict SDKs may reject the frame. Upgrade path: defer the 200
            # headers until the first token and return a real HTTP error for
            # pre-text failures.
            event = json.dumps(
                {
                    "error": {
                        "message": f"upstream {backend} error - see proxy stderr",
                        "type": "proxy_error",
                    }
                }
            )
            try:
                self.wfile.write(f"data: {event}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, OSError):
                pass
            return

        if not error_flag[0]:
            try:
                if held:  # short reply, or a refusal we did not retry
                    forward("".join(held), parser)
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

    def _sync_response(self, prompt, model, model_id, session_state, tools=None, backend="claude"):
        """Collect the full response then return it as one JSON object."""
        stream_fn, backend_creds = self._dispatch(backend)
        kwargs = {
            "model": model,
            "stealth": False,
            "on_text": lambda _t: None,  # don't echo the response to the proxy's stdout
        }
        if backend == "claude":
            kwargs["allow_fallback"] = False

        def run_attempt(state):
            return stream_fn(backend_creds, prompt, session_state=state, **kwargs)

        full = run_attempt(session_state)
        if full is None:
            self._error(502, f"Upstream {backend} API returned no response")
            return
        tool_calls = None
        if tools:
            full, tool_calls = parse_tool_calls(full)
            if backend == "chatgpt" and not tool_calls and _looks_like_tool_refusal(full):
                # Prose refusal instead of a tool fence; retry once in a
                # fresh conversation (same reason as the streaming path).
                retry_state = {}
                full = run_attempt(retry_state)
                _adopt_conversation(session_state, retry_state, backend, backend_creds)
                if full is None:
                    self._error(502, f"Upstream {backend} API returned no response")
                    return
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


def run_proxy(creds, port=8765, chatgpt_creds=None, meta_creds=None):
    """Start the OpenAI-compatible proxy on the given port.

    Blocks until interrupted (Ctrl+C).
    Prints the registration commands for Crush and archived opencode.
    ``chatgpt_creds`` / ``meta_creds`` enable the ``chatgpt/`` and ``meta/``
    prefixed models alongside the claude ones; when None those backends are
    disabled.
    """
    # Inject shared state into the handler class
    _Handler.creds = creds
    _Handler.chatgpt_creds = chatgpt_creds
    _Handler.meta_creds = meta_creds
    _Handler.session_registry = {}

    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    # shortcut: concurrent first-turn requests on the same conversation id can
    # each create a server-side conversation (one gets orphaned in the sidebar).
    # Upgrade to per-key locks if that ever shows up in practice.

    print(f"\n[proxy] si OpenAI-compatible proxy listening on http://localhost:{port}")
    print("\n  Register in Crush (~/.config/crush/crushrc):")
    print(f'    provider add si --type openai --base-url "http://localhost:{port}/v1"')
    print('    model add si/sonnet --name "Claude Sonnet (si)" --context-window 200000')
    print('    model add si/haiku --name "Claude Haiku (si)" --context-window 200000')
    if chatgpt_creds is not None:
        print('\n  ChatGPT models enabled (prefix "chatgpt/", e.g. chatgpt/auto):')
        print('    model add chatgpt/auto --name "ChatGPT (si)" --context-window 200000')
    if meta_creds is not None:
        print('\n  Meta AI models enabled (prefix "meta/", e.g. meta/muse-spark):')
        print('    model add meta/muse-spark --name "Meta AI (si)" --context-window 200000')
    print("\n  Or for archived opencode binary:")
    print(f"    LOCAL_ENDPOINT=http://localhost:{port} opencode")
    print("\n  Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[proxy] Shutting down.")
    finally:
        server.server_close()
