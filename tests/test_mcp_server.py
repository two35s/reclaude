"""MCP stdio server: negotiation, tool calls, shared conversation, clean stdout."""

import io
import json

from si import mcp_server
from si.mcp_server import handle_message, serve


def _init(mid=1, version=None):
    params = {} if version is None else {"protocolVersion": version}
    return {"jsonrpc": "2.0", "id": mid, "method": "initialize", "params": params}


def _call(mid, args, name="claude_ask"):
    return {
        "jsonrpc": "2.0",
        "id": mid,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }


def _ask_ok(text):
    return lambda prompt, model: (text, None)


# ── lifecycle ───────────────────────────────────────────────────────────────


def test_initialize_negotiates_supported_version():
    resp = handle_message(_init(version="2025-06-18"))
    result = resp["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"] == "si"


def test_initialize_falls_back_on_unknown_version():
    resp = handle_message(_init(version="1999-01-01"))
    assert resp["result"]["protocolVersion"] == mcp_server.LATEST_PROTOCOL_VERSION


def test_initialize_without_version_uses_latest():
    resp = handle_message(_init())
    assert resp["result"]["protocolVersion"] == mcp_server.LATEST_PROTOCOL_VERSION


def test_notifications_initialized_has_no_response():
    assert handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_ping_returns_empty_result():
    resp = handle_message({"jsonrpc": "2.0", "id": 7, "method": "ping"})
    assert resp == {"jsonrpc": "2.0", "id": 7, "result": {}}


# ── discovery ───────────────────────────────────────────────────────────────


def test_tools_list_exposes_both_schemas():
    resp = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = resp["result"]["tools"]
    assert [t["name"] for t in tools] == ["claude_ask", "chatgpt_ask"]
    claude_schema, chatgpt_schema = tools
    assert claude_schema["inputSchema"]["required"] == ["prompt"]
    assert claude_schema["inputSchema"]["properties"]["model"]["default"] == "sonnet"
    assert chatgpt_schema["inputSchema"]["required"] == ["prompt"]
    assert (
        chatgpt_schema["inputSchema"]["properties"]["model"]["default"]
        == mcp_server.CHATGPT_DEFAULT_MODEL
    )


# ── tools/call ──────────────────────────────────────────────────────────────


def test_tools_call_success():
    resp = handle_message(_call(3, {"prompt": "hi"}), ask=_ask_ok("hello"))
    assert resp["result"] == {"content": [{"type": "text", "text": "hello"}], "isError": False}


def test_tools_call_passes_prompt_and_model():
    seen = {}

    def ask(prompt, model):
        seen.update(prompt=prompt, model=model)
        return "x", None

    handle_message(_call(4, {"prompt": "p", "model": "haiku"}), ask=ask)
    assert seen == {"prompt": "p", "model": "haiku"}


def test_tools_call_backend_error_sets_is_error():
    resp = handle_message(
        _call(5, {"prompt": "hi"}), ask=lambda p, m: (None, "backend error: boom")
    )
    assert resp["result"]["isError"] is True
    assert "boom" in resp["result"]["content"][0]["text"]


def test_tools_call_empty_prompt_is_invalid():
    resp = handle_message(_call(6, {"prompt": "   "}), ask=_ask_ok("never"))
    assert resp["result"]["isError"] is True
    assert "prompt" in resp["result"]["content"][0]["text"]


def test_tools_call_bad_model_type_is_invalid():
    resp = handle_message(_call(7, {"prompt": "hi", "model": 5}), ask=_ask_ok("never"))
    assert resp["result"]["isError"] is True


def test_tools_call_unknown_tool_is_protocol_error():
    resp = handle_message(_call(8, {"prompt": "hi"}, name="nope"))
    assert resp["error"]["code"] == -32602


def test_chatgpt_ask_dispatches_to_chatgpt_backend():
    seen = {}

    def ask_chatgpt(prompt, model):
        seen.update(prompt=prompt, model=model)
        return "cg reply", None

    resp = handle_message(
        _call(10, {"prompt": "hi"}, name="chatgpt_ask"),
        ask=_ask_ok("never"),
        ask_chatgpt=ask_chatgpt,
    )
    assert resp["result"]["isError"] is False
    assert resp["result"]["content"][0]["text"] == "cg reply"
    assert seen == {"prompt": "hi", "model": mcp_server.CHATGPT_DEFAULT_MODEL}


def test_chatgpt_ask_uses_caller_model():
    seen = {}

    def ask_chatgpt(prompt, model):
        seen["model"] = model
        return "ok", None

    handle_message(
        _call(11, {"prompt": "p", "model": "gpt-6"}, name="chatgpt_ask"),
        ask=_ask_ok("never"),
        ask_chatgpt=ask_chatgpt,
    )
    assert seen["model"] == "gpt-6"


def test_unknown_method_is_error():
    resp = handle_message({"jsonrpc": "2.0", "id": 9, "method": "does/not/exist"})
    assert resp["error"]["code"] == -32601


def test_unknown_notification_ignored():
    assert handle_message({"jsonrpc": "2.0", "method": "some/notification"}) is None


# ── backend integration (run_claude) ────────────────────────────────────────


def test_run_siuses_one_conversation(monkeypatch):
    monkeypatch.setattr(mcp_server, "_session", {"conv_id": None, "created": False})
    monkeypatch.setattr(mcp_server, "_load_creds", lambda: object())
    seen = []

    def fake_stream(creds, prompt, model=None, stealth=None, session_state=None, on_text=None):
        seen.append(session_state.get("conv_id"))
        if not session_state.get("conv_id"):  # mirrors stream_prompt's create-once logic
            session_state["conv_id"] = "conv-1"
        return "ok"

    monkeypatch.setattr(mcp_server, "stream_prompt", fake_stream)
    assert mcp_server.run_claude("a", "sonnet") == ("ok", None)
    assert mcp_server.run_claude("b", "sonnet") == ("ok", None)
    # First call created the conversation; the second reused the same conv_id.
    assert seen == [None, "conv-1"]


def test_run_sisolves_model_alias(monkeypatch):
    monkeypatch.setattr(mcp_server, "_load_creds", lambda: object())
    seen = {}

    def fake_stream(creds, prompt, model=None, stealth=None, session_state=None, on_text=None):
        seen["model"] = model
        return "ok"

    monkeypatch.setattr(mcp_server, "stream_prompt", fake_stream)
    mcp_server.run_claude("a", "sonnet")
    assert seen["model"] == "claude-sonnet-5-5"


def test_run_claude_backend_exception(monkeypatch):
    monkeypatch.setattr(mcp_server, "_load_creds", lambda: object())

    def boom(*a, **k):
        raise TimeoutError("slow")

    monkeypatch.setattr(mcp_server, "stream_prompt", boom)
    text, error = mcp_server.run_claude("x", "sonnet")
    assert text is None
    assert "backend error" in error


def test_run_claude_missing_credentials(monkeypatch):
    def no_creds():
        raise RuntimeError("No valid Claude.ai credentials. Run: ...")

    monkeypatch.setattr(mcp_server, "_load_creds", no_creds)
    text, error = mcp_server.run_claude("x", "sonnet")
    assert text is None
    assert "credentials" in error


# ── stdio transport ─────────────────────────────────────────────────────────


def test_serve_keeps_stdout_clean_and_sends_diagnostics_to_stderr(capsys):
    requests = (
        "\n".join(
            [
                json.dumps(_init(1)),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                json.dumps(_call(3, {"prompt": "hi"})),
            ]
        )
        + "\n"
    )

    def noisy_ask(prompt, model):
        print("[!] backend progress on stdout")  # must NOT reach the protocol stream
        return "ok", None

    out = io.StringIO()
    serve(io.StringIO(requests), out, ask=noisy_ask)

    lines = [ln for ln in out.getvalue().split("\n") if ln.strip()]
    parsed = [json.loads(ln) for ln in lines]
    # initialize + tools/list + tools/call replies; the notification yields none.
    assert sorted(p["id"] for p in parsed) == [1, 2, 3]
    assert "backend progress" not in out.getvalue()
    assert "backend progress on stdout" in capsys.readouterr().err


def test_serve_reports_parse_error_and_continues():
    requests = "not json\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
    out = io.StringIO()
    serve(io.StringIO(requests), out, ask=_ask_ok("x"))
    parsed = [json.loads(ln) for ln in out.getvalue().split("\n") if ln.strip()]
    assert parsed[0]["error"]["code"] == -32700
    assert parsed[1]["result"] == {}
