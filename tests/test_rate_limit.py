"""Rate-limit handling: bounded budget, haiku fallback, proxy-mode opt-out."""

import claude_re.streaming as st


class _Resp:
    status_code = 429
    headers = {}
    text = '{"error": "rate limited"}'


def test_budget_exhausted_gives_up(monkeypatch):
    slept = []
    monkeypatch.setattr(st.time, "sleep", slept.append)
    # Already on haiku with an exhausted budget: return None, no more waits.
    out = st.handle_rate_limit(_Resp(), None, "p", "claude-haiku-5-5", True, {}, _retries=2)
    assert out is None


def test_no_fallback_returns_none_without_sleeping(monkeypatch):
    slept = []
    monkeypatch.setattr(st.time, "sleep", slept.append)
    out = st.handle_rate_limit(
        _Resp(), None, "p", "claude-sonnet-5-5", True, {}, allow_fallback=False
    )
    assert out is None
    assert slept == []  # proxy mode: no hidden multi-minute sleep


def test_falls_back_to_haiku_once(monkeypatch):
    seen = {}

    def fake_stream(creds, prompt, model, stealth, session_state=None, **kw):
        seen["model"] = model
        return "ok"

    monkeypatch.setattr(st, "stream_prompt", fake_stream)
    out = st.handle_rate_limit(_Resp(), None, "p", "claude-sonnet-5-5", True, {})
    assert out == "ok"
    assert seen["model"] == st.MODEL_ALIASES["haiku"]
