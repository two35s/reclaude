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


def test_model_id_mapping():
    assert _resolve_model_from_openai_id("reclaude/sonnet") == "claude-sonnet-5-5"
    assert _resolve_model_from_openai_id("haiku") == "claude-haiku-5-5"
    assert _resolve_model_from_openai_id("claude-sonnet-4-6") == "claude-sonnet-4-6"
