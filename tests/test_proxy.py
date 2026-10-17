"""OpenAI translation helpers: prompt building and model-id mapping."""

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
    assert _resolve_model_from_openai_id("reclaude/sonnet") == "claude-sonnet-5-5"
    assert _resolve_model_from_openai_id("haiku") == "claude-haiku-5-5"
    assert _resolve_model_from_openai_id("claude-sonnet-4-6") == "claude-sonnet-4-6"


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

    monkeypatch.setattr(proxy_mod, "stream_prompt", lambda *a, **k: None)
    handler._stream_response("hi", "claude-sonnet-5-5", "reclaude/sonnet", {})

    out = handler.wfile.getvalue().decode()
    assert '"error"' in out
    assert out.rstrip().endswith("data: [DONE]")
