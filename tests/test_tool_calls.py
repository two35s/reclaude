"""Text tool-call emulation: spec rendering, parsing, and streaming parity."""

import json

from si.proxy import _non_stream_response, _openai_messages_to_prompt
from si.tool_calls import (
    FENCE_END,
    FENCE_START,
    ToolCallParser,
    parse_tool_calls,
    render_tool_prompt,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }
]


def _call(name="read", args=None):
    return f"{FENCE_START}\n{json.dumps({'name': name, 'arguments': args or {'path': '/a'}})}\n{FENCE_END}"


def test_render_tool_prompt_includes_name_schema_and_protocol():
    block = render_tool_prompt(TOOLS)
    assert "read" in block and "Read a file" in block
    assert '"path"' in block  # schema is passed through
    assert FENCE_START in block  # protocol shows the exact syntax


def test_render_tool_prompt_empty_when_no_usable_tools():
    assert render_tool_prompt([]) == ""
    assert render_tool_prompt(None) == ""
    assert render_tool_prompt([{"type": "function"}]) == ""


def test_parse_single_call_keeps_surrounding_text():
    text = f"Let me check.{FENCE_START}\n{json.dumps({'name': 'read', 'arguments': {'path': '/a'}})}\n{FENCE_END}\nDone."
    plain, calls = parse_tool_calls(text)
    assert calls[0]["function"]["name"] == "read"
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "/a"}
    assert calls[0]["id"].startswith("call_")
    assert FENCE_START not in plain and FENCE_END not in plain
    assert "Let me check." in plain and "Done." in plain


def test_parse_multiple_calls_in_one_response():
    text = _call("read") + "\n" + _call("bash", {"command": "ls"})
    _, calls = parse_tool_calls(text)
    assert [c["function"]["name"] for c in calls] == ["read", "bash"]


def test_malformed_fence_is_kept_as_text_not_dropped():
    text = f"before {FENCE_START}\nnot json at all\n{FENCE_END} after"
    plain, calls = parse_tool_calls(text)
    assert calls == []
    assert "not json at all" in plain  # surfaced, not silently discarded


def test_unterminated_fence_at_end_is_emitted_as_text():
    text = f"working {FENCE_START}\n{json.dumps({'name': 'read'})}"
    plain, calls = parse_tool_calls(text)
    assert calls == []
    assert FENCE_START in plain


def test_non_object_arguments_are_wrapped():
    text = _call("read", "not-an-object")
    _, calls = parse_tool_calls(text)
    assert json.loads(calls[0]["function"]["arguments"]) == {"value": "not-an-object"}


def test_call_missing_name_is_not_a_call():
    text = f"{FENCE_START}\n{json.dumps({'arguments': {'path': '/a'}})}\n{FENCE_END}"
    plain, calls = parse_tool_calls(text)
    assert calls == []
    assert "arguments" in plain


def test_streaming_matches_one_shot_for_any_chunking():
    """The fence markers must survive being split across network chunks."""
    text = (
        f"I'll read it first.\n{_call('read')}\n"
        f"Then run {_call('bash', {'command': 'ls -la'})}\ntrailing text"
    )
    want_plain, want_calls = parse_tool_calls(text)

    for size in (1, 2, 3, 7, 13, len(text)):
        parser = ToolCallParser()
        out = []
        for i in range(0, len(text), size):
            out.append(parser.feed(text[i : i + size]))
        tail, got_calls = parser.finish()
        got_plain = "".join(out) + tail

        assert got_plain == want_plain, f"chunk={size}"
        assert [c["function"]["name"] for c in got_calls] == [
            c["function"]["name"] for c in want_calls
        ], f"chunk={size}"
        assert [c["function"]["arguments"] for c in got_calls] == [
            c["function"]["arguments"] for c in want_calls
        ], f"chunk={size}"


def test_plain_text_without_tools_is_unchanged():
    parser = ToolCallParser()
    out = parser.feed("just a normal reply about ```python\ncode\n```")
    tail, calls = parser.finish()
    assert out + tail == "just a normal reply about ```python\ncode\n```"
    assert calls == []


def test_prompt_renders_tool_spec_into_system_block():
    prompt = _openai_messages_to_prompt(
        [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}],
        tools=TOOLS,
    )
    assert prompt.startswith("[System instructions]:\nbe brief")
    assert "[Available tools]" in prompt and "read" in prompt


def test_prompt_without_tools_has_no_tool_block():
    prompt = _openai_messages_to_prompt([{"role": "user", "content": "hi"}])
    assert "[Available tools]" not in prompt


def test_prompt_replays_prior_calls_and_folds_tool_results():
    prompt = _openai_messages_to_prompt(
        [
            {"role": "user", "content": "read a"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read", "arguments": '{"path": "/a"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "read", "content": "file body"},
            {"role": "user", "content": "thanks"},
        ],
        tools=TOOLS,
    )
    assert FENCE_START in prompt  # own prior call replayed in the same syntax
    assert "[Tool result read]: file body" in prompt
    assert prompt.rstrip().endswith("[User]: thanks")


def test_non_stream_response_sets_tool_calls_finish_reason():
    _, calls = parse_tool_calls(_call("read"))
    resp = _non_stream_response("checking", "m", calls)
    choice = resp["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "read"
    assert choice["message"]["content"] == "checking"

    plain = _non_stream_response("just text", "m")
    assert plain["choices"][0]["finish_reason"] == "stop"
    assert "tool_calls" not in plain["choices"][0]["message"]
