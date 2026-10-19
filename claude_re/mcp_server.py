"""Minimal stdlib-only MCP (stdio) server exposing `claude_ask` and `chatgpt_ask`.

Speaks newline-delimited JSON-RPC 2.0 on stdin/stdout, per the Model Context
Protocol stdio transport. The real stdout is reserved for protocol messages;
reclaude's own progress prints go to stderr, because the streaming backend
writes to stdout with bare print() and would otherwise corrupt the stream.

Run:      python -m claude_re.mcp_server
Register: see the "MCP server (OpenCode)" section in README.md.
"""

import json
import sys
import threading

from . import __version__
from .config import CHATGPT_DEFAULT_MODEL, CHATGPT_MODELS, resolve_model
from .creds import CredentialManager
from .streaming import stream_prompt

SERVER_NAME = "reclaude"
SERVER_VERSION = __version__

# Revisions this server can speak. OpenCode's default "legacy" negotiation
# opens with an initialize handshake and accepts revisions up to 2025-11-25.
LATEST_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")

TOOL_NAME = "claude_ask"
CHATGPT_TOOL_NAME = "chatgpt_ask"
DEFAULT_MODEL_ALIAS = "sonnet"

# One backend conversation per provider for the server's whole lifetime
# (shared memory), and a lock so concurrent calls cannot interleave writes.
_session = {"conv_id": None, "created": False}
_chatgpt_session = {"conv_id": None, "created": False}
_lock = threading.Lock()


def _load_creds():
    """Load Claude.ai credentials from disk (no caching, so re-login takes effect)."""
    creds = CredentialManager()
    if not creds.load() or not creds.is_valid():
        raise RuntimeError("No valid Claude.ai credentials. Run: python -m claude_re --auto-fetch")
    return creds


def _load_chatgpt_creds():
    """Load ChatGPT credentials from disk (no caching, so re-login takes effect)."""
    from .chatgpt_creds import ChatGPTCredentials

    creds = ChatGPTCredentials()
    if not creds.load() or not creds.is_valid():
        raise RuntimeError(
            "No valid ChatGPT credentials. Run: python -m claude_re --chatgpt --from-chrome"
        )
    return creds


def run_claude(prompt, model):
    """Call the reclaude backend. Returns (text, None) or (None, error_message)."""
    try:
        creds = _load_creds()
    except RuntimeError as e:
        return None, str(e)
    model_id = resolve_model(model)
    with _lock:  # serialize: stream_prompt mutates the shared conversation state
        try:
            text = stream_prompt(
                creds,
                prompt,
                model=model_id,
                stealth=False,
                session_state=_session,
                on_text=lambda _t: None,  # suppress live echo; result is returned below
            )
        except Exception as e:
            return None, f"backend error: {e}"
    if text is None:
        return None, (
            "No response from Claude.ai (expired session, rate limit, or model "
            "unavailable). See the MCP server's stderr for the reason."
        )
    return text, None


def run_chatgpt(prompt, model):
    """Call the ChatGPT backend. Returns (text, None) or (None, error_message)."""
    from . import chatgpt_stream

    try:
        creds = _load_chatgpt_creds()
    except RuntimeError as e:
        return None, str(e)
    if model not in CHATGPT_MODELS:
        return None, f"Unknown ChatGPT model {model!r}. Available: {', '.join(CHATGPT_MODELS)}"
    with _lock:  # serialize: the shared conversation state is not reentrant
        try:
            text = chatgpt_stream.stream_prompt(
                creds,
                prompt,
                model=model,
                stealth=False,
                session_state=_chatgpt_session,
                on_text=lambda _t: None,  # suppress live echo; result is returned below
            )
        except Exception as e:
            return None, f"backend error: {e}"
    if text is None:
        return None, (
            "No response from ChatGPT (expired session, rate limit, or turn "
            "refused). See the MCP server's stderr for the reason."
        )
    return text, None


# ── JSON-RPC / MCP shaping ──────────────────────────────────────────────────


def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_text(mid, text, is_error=False):
    return _result(mid, {"content": [{"type": "text", "text": text}], "isError": is_error})


def _tool_schema(name, description, prompt_description, model_description, default_model):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": prompt_description,
                },
                "model": {
                    "type": "string",
                    "description": model_description,
                    "default": default_model,
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    }


def _claude_schema():
    return _tool_schema(
        TOOL_NAME,
        "Ask Claude.ai (through the local reclaude browser session) a question "
        "and return its text reply. Use it for a second opinion or when the user "
        "asks for Claude specifically.",
        "The question or instruction to send to Claude.ai.",
        "Model alias (sonnet, haiku, opus) or full model id.",
        DEFAULT_MODEL_ALIAS,
    )


def _chatgpt_schema():
    return _tool_schema(
        CHATGPT_TOOL_NAME,
        "Ask ChatGPT (through the local reclaude browser session) a question "
        "and return its text reply. Use it for a second opinion or when the "
        "user asks for ChatGPT specifically.",
        "The question or instruction to send to ChatGPT.",
        f"ChatGPT model slug. Available: {', '.join(CHATGPT_MODELS)}.",
        CHATGPT_DEFAULT_MODEL,
    )


def _handle_tools_call(mid, params, ask, ask_chatgpt=run_chatgpt):
    name = params.get("name")
    if name == CHATGPT_TOOL_NAME:
        runner, default_model = ask_chatgpt, CHATGPT_DEFAULT_MODEL
    elif name == TOOL_NAME:
        runner, default_model = ask, DEFAULT_MODEL_ALIAS
    else:
        return _error(mid, -32602, f"Unknown tool: {name!r}")
    args = params.get("arguments")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _error(mid, -32602, "Invalid params: 'arguments' must be an object")

    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _tool_text(mid, "Invalid input: 'prompt' must be a non-empty string.", True)
    model = args.get("model", default_model)
    if not isinstance(model, str) or not model.strip():
        return _tool_text(mid, "Invalid input: 'model' must be a non-empty string.", True)

    text, error = runner(prompt, model)
    if error is not None:
        return _tool_text(mid, error, True)
    return _tool_text(mid, text)


def handle_message(msg, ask=run_claude, ask_chatgpt=run_chatgpt):
    """Handle one decoded JSON-RPC message.

    Returns a response dict, or None for notifications / messages needing no reply.
    """
    if not isinstance(msg, dict):
        return _error(None, -32600, "Invalid Request")
    mid = msg.get("id")
    method = msg.get("method")
    if not isinstance(method, str):
        return None  # a response (or malformed message) from the client: nothing to do

    if method == "initialize":
        requested = (msg.get("params") or {}).get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        return _result(
            mid,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": [_claude_schema(), _chatgpt_schema()]})
    if method == "tools/call":
        params = msg.get("params")
        if not isinstance(params, dict):
            return _error(mid, -32602, "Invalid params")
        return _handle_tools_call(mid, params, ask, ask_chatgpt)

    if mid is None:
        return None  # unknown notification: ignore
    return _error(mid, -32601, f"Method not found: {method}")


# ── stdio transport ─────────────────────────────────────────────────────────


def _write(obj, out):
    out.write(json.dumps(obj) + "\n")
    out.flush()


def serve(instream, outstream, ask=run_claude, ask_chatgpt=run_chatgpt):
    """Read newline-delimited JSON-RPC from ``instream``, write replies to ``outstream``.

    sys.stdout is redirected to stderr for the duration so reclaude's own prints
    never reach the protocol stream. Requests are handled sequentially, which
    (with the lock in the runners) also keeps the shared conversations safe.
    """
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        for line in instream:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                _write(_error(None, -32700, "Parse error"), outstream)
                continue
            response = handle_message(msg, ask, ask_chatgpt)
            if response is not None:
                _write(response, outstream)
    finally:
        sys.stdout = real_stdout


def main():
    serve(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
