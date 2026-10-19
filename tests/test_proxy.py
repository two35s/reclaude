"""OpenAI translation helpers: prompt building and model-id mapping."""

import json

import pytest

from si.proxy import (
    _looks_like_tool_refusal,
    _openai_messages_to_prompt,
    _resolve_model_from_openai_id,
)


def test_system_prepended_and_turns_keep_order():
    prompt = _openai_messages_to_prompt(
        [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hey"},
            {"role": "user", "content": "yo"},
        ]
    )
    assert prompt.startswith("[System instructions]:\nbe brief")
    assert "[User]: hi" in prompt and "[Assistant]: hey" in prompt
    assert prompt.rstrip().endswith("[User]: yo")


def test_vision_block_list_keeps_text_only():
    prompt = _openai_messages_to_prompt(
        [{"role": "user", "content": [{"type": "text", "text": "see"}, {"type": "image_url"}]}]
    )
    assert "[User]: see" in prompt


def test_recent_only_keeps_system_drops_early_turns():
    prompt = _openai_messages_to_prompt(
        [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "new question"},
        ],
        recent_only=True,
    )
    assert "be brief" in prompt
    assert "old question" not in prompt and "old answer" not in prompt
    assert prompt.rstrip().endswith("[User]: new question")


def test_recent_only_false_keeps_full_transcript():
    prompt = _openai_messages_to_prompt(
        [
            {"role": "user", "content": "old question"},
            {"role": "user", "content": "new question"},
        ],
        recent_only=False,
    )
    assert "old question" in prompt and "new question" in prompt


def test_model_id_mapping():
    assert _resolve_model_from_openai_id("si/sonnet") == ("claude", "claude-sonnet-5-5")
    assert _resolve_model_from_openai_id("haiku") == ("claude", "claude-haiku-5-5")
    assert _resolve_model_from_openai_id("claude-sonnet-4-6") == ("claude", "claude-sonnet-4-6")


def test_chatgpt_model_id_mapping():
    assert _resolve_model_from_openai_id("chatgpt/auto") == ("chatgpt", "auto")
    assert _resolve_model_from_openai_id("chatgpt/gpt-6") == ("chatgpt", "gpt-6")
    assert _resolve_model_from_openai_id("si/chatgpt/auto") == ("chatgpt", "auto")


def test_stream_response_sends_error_chunk_when_upstream_fails(monkeypatch):
    # Regression guard for issue #3: a failed upstream must not read as an
    # empty 200 with finish_reason stop.
    import io

    import si.proxy as proxy_mod

    handler = object.__new__(proxy_mod._Handler)
    handler.wfile = io.BytesIO()
    handler.send_response = lambda *a, **k: None
    handler.send_header = lambda *a, **k: None
    handler.end_headers = lambda *a, **k: None
    handler.creds = None

    monkeypatch.setattr(proxy_mod, "claude_stream_prompt", lambda *a, **k: None)
    handler._stream_response("hi", "claude-sonnet-5-5", "si/sonnet", {})

    out = handler.wfile.getvalue().decode()
    assert "upstream claude error" in out
    assert out.rstrip().endswith("data: [DONE]")
    # every data frame must be parseable — OpenCode rejects the whole
    # stream on one malformed frame (issue: "Invalid ... stream event")
    for line in out.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            assert isinstance(json.loads(line[len("data: ") :]), dict)


def test_stream_response_routes_chatgpt_backend(monkeypatch):
    # chatgpt/ models must reach the chatgpt backend, never the claude one.
    import io

    import si.proxy as proxy_mod

    handler = object.__new__(proxy_mod._Handler)
    handler.wfile = io.BytesIO()
    handler.send_response = lambda *a, **k: None
    handler.send_header = lambda *a, **k: None
    handler.end_headers = lambda *a, **k: None
    handler.creds = None
    handler.chatgpt_creds = object()

    seen = {}

    def fake_chatgpt(creds, prompt, **kwargs):
        seen["creds"] = creds
        seen["prompt"] = prompt
        kwargs["on_text"]("hello from chatgpt")
        return "hello from chatgpt"

    def wrong_backend(*a, **k):
        raise AssertionError("claude backend must not receive chatgpt/ models")

    import si.chatgpt_stream as chatgpt_mod

    monkeypatch.setattr(proxy_mod, "claude_stream_prompt", wrong_backend)
    monkeypatch.setattr(chatgpt_mod, "stream_prompt", fake_chatgpt)
    handler._stream_response("hi", "auto", "chatgpt/auto", {}, backend="chatgpt")

    assert seen["creds"] is handler.chatgpt_creds
    assert "hello from chatgpt" in handler.wfile.getvalue().decode()


def test_refusal_detection_stays_inside_the_hold_window():
    assert _looks_like_tool_refusal(
        "I couldn't execute the requested edit in the OpenCode workspace."
    )
    assert _looks_like_tool_refusal("The requested edit tool isn't exposed in this chat's tools.")
    assert not _looks_like_tool_refusal("I'll update demo.py now.\n\n```tool_call")
    assert not _looks_like_tool_refusal("Done.")
    assert not _looks_like_tool_refusal("x" * 300 + " I can't do that")  # past the window


def _stub_handler():
    import io

    import si.proxy as proxy_mod

    handler = object.__new__(proxy_mod._Handler)
    handler.wfile = io.BytesIO()
    handler.send_response = lambda *a, **k: None
    handler.send_header = lambda *a, **k: None
    handler.end_headers = lambda *a, **k: None
    handler.chatgpt_creds = object()
    return handler


def test_chatgpt_prose_refusal_is_retried_invisibly(monkeypatch):
    # Regression guard for "chatgpt can't edit on opencode": a prose refusal
    # on a tool-bearing turn must be replaced by a retry, not shown.
    import si.chatgpt_stream as chatgpt_mod

    refusal = "I can't reach the OpenCode workspace with the tools available in this session."
    fence = '```tool_call\n{"name": "read", "arguments": {"path": "demo.py"}}\n```'
    replies = [refusal, fence]
    states = []

    def fake_chatgpt(creds, prompt, **kwargs):
        states.append(kwargs["session_state"])
        text = replies.pop(0)
        kwargs["on_text"](text)
        return text

    monkeypatch.setattr(chatgpt_mod, "stream_prompt", fake_chatgpt)
    handler = _stub_handler()
    handler._stream_response(
        "hi",
        "auto",
        "chatgpt/auto",
        {"first": True},
        tools=[{"type": "function", "function": {"name": "read"}}],
        backend="chatgpt",
    )

    out = handler.wfile.getvalue().decode()
    assert "can't" not in out  # the refusal never reached the client
    assert '"name": "read"' in out and '"tool_calls"' in out
    assert states == [{"first": True}, {}]  # retry ran in a fresh conversation


def test_chatgpt_shows_second_refusal_when_retry_also_refuses(monkeypatch):
    import si.chatgpt_stream as chatgpt_mod

    first = "I cannot access that workspace."
    second = "I'm unable to edit files here."
    replies = [first, second]

    def fake_chatgpt(creds, prompt, **kwargs):
        text = replies.pop(0)
        kwargs["on_text"](text)
        return text

    monkeypatch.setattr(chatgpt_mod, "stream_prompt", fake_chatgpt)
    handler = _stub_handler()
    handler._stream_response(
        "hi",
        "auto",
        "chatgpt/auto",
        {},
        tools=[{"type": "function", "function": {"name": "read"}}],
        backend="chatgpt",
    )

    out = handler.wfile.getvalue().decode()
    assert first not in out
    assert "unable to edit" in out


def test_sync_chatgpt_refusal_is_retried(monkeypatch):
    import si.chatgpt_stream as chatgpt_mod

    replies = [
        "I couldn't reach the workspace.",
        '```tool_call\n{"name": "read", "arguments": {"path": "demo.py"}}\n```',
    ]
    captured = {}

    def fake_chatgpt(creds, prompt, **kwargs):
        return replies.pop(0)

    monkeypatch.setattr(chatgpt_mod, "stream_prompt", fake_chatgpt)
    handler = _stub_handler()
    handler._send = lambda code, ctype, body: captured.update(code=code, body=json.loads(body))
    handler._sync_response(
        "hi",
        "auto",
        "chatgpt/auto",
        {},
        tools=[{"type": "function", "function": {"name": "read"}}],
        backend="chatgpt",
    )

    message = captured["body"]["choices"][0]["message"]
    assert message["tool_calls"][0]["function"]["name"] == "read"


def test_registry_cap_evicts_oldest_and_keeps_new_key():
    import io

    import si.proxy as proxy_mod

    body = json.dumps(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "si/sonnet",
            "stream": True,
        }
    ).encode()
    handler = object.__new__(proxy_mod._Handler)
    handler.headers = {"Content-Length": str(len(body)), "X-Conversation-Id": "new-key"}
    handler.rfile = io.BytesIO(body)
    handler.session_registry = {("claude", f"old-{i}"): {} for i in range(256)}
    handler.registry_lock = proxy_mod.threading.Lock()
    captured = {}
    # stub out the actual completion call, keep the session_state it received
    handler._stream_response = lambda prompt, model, model_id, state, tools=None, backend="claude": (
        captured.update(state=state)
    )

    handler._chat_completions()

    assert ("claude", "old-0") not in handler.session_registry  # oldest evicted
    assert ("claude", "new-key") in handler.session_registry and handler.session_registry[
        ("claude", "new-key")
    ] is captured["state"]
    assert len(handler.session_registry) == 256


def _request_handler(body, headers=None):
    import io

    import si.proxy as proxy_mod

    handler = object.__new__(proxy_mod._Handler)
    handler.headers = {"Content-Length": str(len(body)), **(headers or {})}
    handler.rfile = io.BytesIO(body)
    errors = []
    handler._error = lambda code, msg: errors.append((code, msg))
    handler._stream_response = lambda *a, **k: errors.append(("stream", None))
    handler._sync_response = lambda *a, **k: errors.append(("sync", None))
    return handler, errors


@pytest.mark.parametrize(
    "body",
    [
        b"{not json",
        b"[]",
        json.dumps({"model": 123, "messages": [{"role": "user", "content": "hi"}]}).encode(),
        json.dumps({"messages": "hi"}).encode(),
        json.dumps({"messages": [["bad"]]}).encode(),
        json.dumps({"messages": []}).encode(),
    ],
)
def test_chat_completions_rejects_malformed_fields(body):
    handler, errors = _request_handler(body)
    handler._chat_completions()
    assert errors and errors[0][0] == 400
    assert not any(kind in ("stream", "sync") for kind, _ in errors)


def test_keyless_request_with_user_field_is_deleted(monkeypatch):
    import si.proxy as proxy_mod

    body = json.dumps(
        {
            "model": "si/sonnet",
            "user": "analytics-user",  # not a conversation id
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()
    handler, errors = _request_handler(body)
    handler.creds = object()
    handler.chatgpt_creds = None
    handler.session_registry = {}
    handler.registry_lock = proxy_mod.threading.Lock()
    deleted = []
    monkeypatch.setattr(proxy_mod, "_delete_conversation", lambda *a: deleted.append(a))

    def fake_stream(prompt, model, model_id, state, tools=None, backend="claude"):
        state["conv_id"] = "one-shot"

    handler._stream_response = fake_stream
    handler._chat_completions()

    assert errors == []
    assert handler.session_registry == {}  # `user` must not become a conversation key
    assert deleted == [("claude", handler.creds, "one-shot")]


def test_keyed_request_registers_and_keeps_conversation(monkeypatch):
    import si.proxy as proxy_mod

    body = json.dumps(
        {
            "model": "si/sonnet",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()
    handler, errors = _request_handler(body, {"X-Conversation-Id": "chat-1"})
    handler.creds = object()
    handler.chatgpt_creds = None
    handler.session_registry = {}
    handler.registry_lock = proxy_mod.threading.Lock()
    deleted = []
    monkeypatch.setattr(proxy_mod, "_delete_conversation", lambda *a: deleted.append(a))

    def fake_stream(prompt, model, model_id, state, tools=None, backend="claude"):
        state["conv_id"] = "kept"

    handler._stream_response = fake_stream
    handler._chat_completions()

    assert errors == [] and deleted == []
    assert handler.session_registry[("claude", "chat-1")]["conv_id"] == "kept"


def test_streaming_retry_adopts_the_new_conversation(monkeypatch):
    import si.chatgpt_stream as chatgpt_mod
    import si.proxy as proxy_mod

    refusal = "I can't reach the OpenCode workspace."
    fence = '```tool_call\n{"name": "read", "arguments": {"path": "demo.py"}}\n```'
    replies = [refusal, fence]
    seen = []
    deleted = []
    monkeypatch.setattr(proxy_mod, "_delete_conversation", lambda *a: deleted.append(a))

    def fake_chatgpt(creds, prompt, **kwargs):
        state = kwargs["session_state"]
        seen.append(dict(state))
        if not state.get("conv_id"):
            state["conv_id"] = f"conv-{len(seen)}"
        text = replies.pop(0)
        kwargs["on_text"](text)
        return text

    monkeypatch.setattr(chatgpt_mod, "stream_prompt", fake_chatgpt)
    handler = _stub_handler()
    shared = {"conv_id": "old"}
    handler._stream_response(
        "hi",
        "auto",
        "chatgpt/auto",
        shared,
        tools=[{"type": "function", "function": {"name": "read"}}],
        backend="chatgpt",
    )

    assert shared["conv_id"] == "conv-2"
    assert seen == [{"conv_id": "old"}, {}]
    assert deleted == [("chatgpt", handler.chatgpt_creds, "old")]


def test_sync_retry_adopts_the_new_conversation(monkeypatch):
    import si.chatgpt_stream as chatgpt_mod
    import si.proxy as proxy_mod

    replies = [
        "I couldn't reach the workspace.",
        '```tool_call\n{"name": "read", "arguments": {"path": "demo.py"}}\n```',
    ]
    seen = []
    deleted = []
    monkeypatch.setattr(proxy_mod, "_delete_conversation", lambda *a: deleted.append(a))

    def fake_chatgpt(creds, prompt, **kwargs):
        state = kwargs["session_state"]
        seen.append(dict(state))
        if not state.get("conv_id"):
            state["conv_id"] = f"conv-{len(seen)}"
        return replies.pop(0)

    monkeypatch.setattr(chatgpt_mod, "stream_prompt", fake_chatgpt)
    handler = _stub_handler()
    handler._send = lambda code, ctype, body: None
    shared = {"conv_id": "old"}
    handler._sync_response(
        "hi",
        "auto",
        "chatgpt/auto",
        shared,
        tools=[{"type": "function", "function": {"name": "read"}}],
        backend="chatgpt",
    )

    assert shared["conv_id"] == "conv-2"
    assert deleted == [("chatgpt", handler.chatgpt_creds, "old")]
