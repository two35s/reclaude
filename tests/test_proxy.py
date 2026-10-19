"""OpenAI translation helpers: prompt building and model-id mapping."""

import json

from claude_re.proxy import _openai_messages_to_prompt, _resolve_model_from_openai_id


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
    assert _resolve_model_from_openai_id("reclaude/sonnet") == ("claude", "claude-sonnet-5-5")
    assert _resolve_model_from_openai_id("haiku") == ("claude", "claude-haiku-5-5")
    assert _resolve_model_from_openai_id("claude-sonnet-4-6") == ("claude", "claude-sonnet-4-6")


def test_chatgpt_model_id_mapping():
    assert _resolve_model_from_openai_id("chatgpt/auto") == ("chatgpt", "auto")
    assert _resolve_model_from_openai_id("chatgpt/gpt-6") == ("chatgpt", "gpt-6")


def test_stream_response_sends_error_chunk_when_upstream_fails(monkeypatch):
    # Regression guard for issue #3: a failed upstream must not read as an
    # empty 200 with finish_reason stop.
    import io

    import claude_re.proxy as proxy_mod

    handler = object.__new__(proxy_mod._Handler)
    handler.wfile = io.BytesIO()
    handler.send_response = lambda *a, **k: None
    handler.send_header = lambda *a, **k: None
    handler.end_headers = lambda *a, **k: None
    handler.creds = None

    monkeypatch.setattr(proxy_mod, "claude_stream_prompt", lambda *a, **k: None)
    handler._stream_response("hi", "claude-sonnet-5-5", "reclaude/sonnet", {})

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

    import claude_re.proxy as proxy_mod

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

    import claude_re.chatgpt_stream as chatgpt_mod

    monkeypatch.setattr(proxy_mod, "claude_stream_prompt", wrong_backend)
    monkeypatch.setattr(chatgpt_mod, "stream_prompt", fake_chatgpt)
    handler._stream_response("hi", "auto", "chatgpt/auto", {}, backend="chatgpt")

    assert seen["creds"] is handler.chatgpt_creds
    assert "hello from chatgpt" in handler.wfile.getvalue().decode()


def test_registry_cap_evicts_oldest_and_keeps_new_key():
    import io

    import claude_re.proxy as proxy_mod

    body = json.dumps(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "reclaude/sonnet",
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
