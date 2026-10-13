"""SSE parsing: multibyte chars survive chunk splits; only complete lines pass through."""

from claude_re.streaming import _split_lines, _sse_data


def test_multibyte_char_split_across_chunks():
    # A UTF-8 'é' (two bytes) split across two network chunks must survive.
    payload = 'data: {"text":"é"}\n'.encode()
    cut = payload.index(b"\xc3") + 1
    buf, lines = _split_lines(b"", payload[:cut])
    assert lines == []
    buf, lines = _split_lines(buf, payload[cut:])
    assert len(lines) == 1
    assert _sse_data(lines[0]) == '{"text":"é"}'


def test_split_lines_keeps_only_complete_lines():
    buf, lines = _split_lines(b"", b"a\nb\nc")
    assert lines == [b"a", b"b"]
    assert buf == b"c"


def test_sse_data_ignores_non_data_lines():
    assert _sse_data(b"") is None
    assert _sse_data(b": keep-alive") is None
    assert _sse_data(b"event: message_stop") is None
    assert _sse_data(b"data: [DONE]") == "[DONE]"
