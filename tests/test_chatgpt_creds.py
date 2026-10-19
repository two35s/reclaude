"""ChatGPT credentials: session-token sharding reassembly."""

from claude_re.chatgpt_creds import SESSION_COOKIE, reassemble_session_token


def test_unsharded_token_returned_unchanged():
    cookies = {SESSION_COOKIE: "abc123"}
    assert reassemble_session_token(cookies) == "abc123"


def test_shards_joined_in_numeric_order():
    cookies = {
        f"{SESSION_COOKIE}.2": "-C",
        SESSION_COOKIE: "A",
        f"{SESSION_COOKIE}.1": "-B",
    }
    assert reassemble_session_token(cookies) == "A-B-C"


def test_missing_token_is_empty_string():
    assert reassemble_session_token({}) == ""
    assert reassemble_session_token({"other": "x"}) == ""
