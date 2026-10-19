"""ChatGPT backend: sentinel PoW, fingerprint blobs, SSE delta folding."""

import base64
import hashlib
import json

from claude_re import chatgpt_stream as cs

# ── fingerprint / blob encoding ─────────────────────────────────────────────


def test_build_fingerprint_is_25_fields_with_dynamic_slots():
    arr = cs.build_fingerprint(nonce=42, now_ms=1234.0)
    assert len(arr) == 25
    assert arr[3] == 42
    assert arr[17] == 1234.0
    assert arr[4].startswith("Mozilla/5.0")
    assert arr[6] == "bfc3a09a49ef7874df485c8f0020f40819fb44fb"


def test_encode_blob_roundtrips_to_json_array():
    blob = cs.encode_blob(cs.build_fingerprint(nonce=7))
    raw = base64.b64decode(blob[len(cs.FINGERPRINT_PREFIX) :])
    arr = json.loads(raw)
    assert arr[3] == 7


# ── proof-of-work ───────────────────────────────────────────────────────────


def _pow_fixture(difficulty="0fffff", seed="test-seed"):
    """Server-side check mirroring the sentinel algorithm."""
    target = bytes.fromhex(difficulty)

    def accepts(answer_token):
        assert answer_token.startswith(cs.POW_ANSWER_PREFIX)
        assert answer_token.endswith(cs.POW_ANSWER_SUFFIX)
        body = answer_token[len(cs.POW_ANSWER_PREFIX) : -len(cs.POW_ANSWER_SUFFIX)]
        digest = hashlib.sha3_512(seed.encode() + body.encode()).digest()
        return digest[: len(difficulty)] <= target

    return seed, difficulty, accepts


def test_solve_pow_produces_accepted_answer():
    seed, difficulty, accepts = _pow_fixture()
    token = cs.solve_pow(seed, difficulty)
    assert token is not None
    assert accepts(token)


def test_solve_pow_nonce_lands_at_field_three():
    seed, difficulty, accepts = _pow_fixture()
    token = cs.solve_pow(seed, difficulty)
    body = token[len(cs.POW_ANSWER_PREFIX) : -len(cs.POW_ANSWER_SUFFIX)]
    arr = json.loads(base64.b64decode(body))
    assert arr[3] >= 0  # the nonce slot
    assert accepts(token)


def test_solve_pow_rejects_bad_inputs():
    assert cs.solve_pow("", "0fffff") is None
    assert cs.solve_pow("seed", "") is None
    assert cs.solve_pow("seed", "not-hex") is None


def test_solve_pow_respects_nonce_ceiling(monkeypatch):
    # A difficulty of all-ff is met by any digest, so only by shrinking the
    # ceiling to zero can the give-up path be exercised.
    monkeypatch.setattr(cs, "POW_MAX_NONCE", 0)
    assert cs.solve_pow("seed", "ffffffff") is None


# ── SSE folding ─────────────────────────────────────────────────────────────


def _delta_event(content, role="assistant", status="in_progress"):
    msg = {
        "id": "m1",
        "author": {"role": role, "name": None, "metadata": {}},
        "content": {"content_type": "text", "parts": [content]},
        "status": status,
    }
    return f"event: delta\ndata: {json.dumps({'v': {'message': msg}})}\n"


def test_consume_event_emits_text_tail_from_snapshots():
    parts, emitted = [], []

    assert cs._consume_event(_delta_event("Hel"), parts, emitted.append, {}) is False
    assert cs._consume_event(_delta_event("Hello"), parts, emitted.append, {}) is False
    assert "".join(parts) == "Hello"
    assert emitted == ["Hel", "lo"]  # snapshots carry full text; only tails emit


def test_consume_event_replaces_text_on_wholesale_rewrite():
    parts, emitted = [], []
    cs._consume_event(_delta_event("draft"), parts, emitted.append, {})
    cs._consume_event(_delta_event("final answer"), parts, emitted.append, {})
    assert "".join(parts) == "final answer"
    assert emitted == ["draft", "final answer"]  # rewrite is re-emitted whole


def test_consume_event_user_status_does_not_end_turn():
    # User/system snapshots also carry finished_successfully; only the
    # assistant's terminal snapshot may end the turn.
    parts, state = [], {}
    cs._consume_event(_delta_event("part"), parts, lambda _t: None, state)
    done = cs._consume_event(
        _delta_event("user text", role="user", status="finished_successfully"),
        parts,
        lambda _t: None,
        state,
    )
    assert done is False
    assert "".join(parts) == "part"


def test_consume_event_terminal_status_recorded_but_turn_not_ended():
    # Assistant snapshots can carry a terminal status while text is still
    # streaming; ending there truncates, so only the status is recorded.
    parts, state = [], {}
    cs._consume_event(_delta_event("answer"), parts, lambda _t: None, state)
    done = cs._consume_event(
        _delta_event("answer plus more", status="finished_successfully"),
        parts,
        lambda _t: None,
        state,
    )
    assert done is False
    assert state["status"] == "finished_successfully"
    assert "".join(parts) == "answer plus more"


def test_consume_event_in_band_error_ends_turn():
    event = f"data: {json.dumps({'v': {'error': 'quota exceeded', 'message': None}})}"
    state = {}
    assert cs._consume_event(event, [], lambda _t: None, state) is True
    assert state["error"] == "quota exceeded"


def test_consume_event_json_patch_status_recorded_only():
    # The patch can precede the final text snapshot, so it must not end the turn.
    event = f"data: {json.dumps([{'op': 'replace', 'p': '/message/status', 'v': 'error'}])}"
    parts, state = ["text"], {}
    assert cs._consume_event(event, parts, lambda _t: None, state) is False
    assert state["status"] == "error"


def test_consume_event_patch_appends_text():
    # Later turns stream text as JSON-patch appends, not snapshots.
    parts, emitted = [], []
    first = f"data: {json.dumps({'o': 'patch', 'v': [{'p': '/message/content/parts/0', 'o': 'append', 'v': 'KK'}]})}"
    second = f"data: {json.dumps({'o': 'patch', 'v': [{'p': '/message/content/parts/0', 'o': 'append', 'v': '-4'}]})}"
    assert cs._consume_event(first, parts, emitted.append, {}) is False
    assert cs._consume_event(second, parts, emitted.append, {}) is False
    assert "".join(parts) == "KK-4"
    assert emitted == ["KK", "-4"]


def test_consume_event_patch_replace_rewrites_whole_text():
    parts, emitted = [], []
    event = f"data: {json.dumps([{'p': '/message/content/parts/0', 'o': 'replace', 'v': 'fresh'}])}"
    cs._consume_event(event, parts, emitted.append, {})
    assert "".join(parts) == "fresh"
    assert emitted == ["fresh"]


def test_consume_event_done_and_complete_end_turn():
    assert cs._consume_event("data: [DONE]", [], lambda _t: None, {}) is True
    event = f"data: {json.dumps({'type': 'message_stream_complete', 'conversation_id': 'c1'})}"
    assert cs._consume_event(event, [], lambda _t: None, {}) is True


# ── cleanup ─────────────────────────────────────────────────────────────────


def test_delete_conversation_sends_delete_via_chatgpt_session(monkeypatch):
    calls = []

    class FakeSession:
        def delete(self, url, headers=None, timeout=None):
            calls.append((url, headers))

    class FakeCreds:
        def make_headers(self):
            return {"Cookie": "session=tok"}

        def auth_headers(self):
            return {"Authorization": "Bearer at"}

    monkeypatch.setattr(cs, "_get_session", lambda: FakeSession())
    cs.delete_conversation(FakeCreds(), "conv-9")
    assert calls == [
        (
            "https://chatgpt.com/backend-api/conversation/conv-9",
            {"Cookie": "session=tok", "Authorization": "Bearer at"},
        )
    ]
