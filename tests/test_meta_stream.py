"""Meta AI backend: protobuf frames, WS URL and response folding."""

import base64
import json

from si import meta_stream as meta


def test_conversation_id_shape():
    value = meta.generate_conversation_id()
    assert value.startswith("c.")
    assert len(value) == 21


def test_parse_ws_response_events_handles_nested_and_escaped_json():
    payload = (
        'noise{"type":"full","response":{"sections":[{"view_model":'
        '{"primitive":{"text":"a } b \\" quote"}}}]}}'
        'trailer{"type":"patch","operations":[{"op":"delta","value":"x"}]}'
    )
    events = meta.parse_ws_response_events(payload)
    assert [e["type"] for e in events] == ["full", "patch"]
    assert (
        events[0]["response"]["sections"][0]["view_model"]["primitive"]["text"] == 'a } b " quote'
    )


def test_event_text_folds_snapshot_then_patch():
    full = {
        "type": "full",
        "response": {"sections": [{"view_model": {"primitive": {"text": "Hello"}}}]},
    }
    patch = {
        "type": "patch",
        "operations": [
            {"op": "delta", "path": "/sections/0/view_model/primitive/text", "value": " world"}
        ],
    }
    delta, text = meta._event_text([full], "")
    assert (delta, text) == ("Hello", "Hello")
    delta, text = meta._event_text([patch], text)
    assert (delta, text) == (" world", "Hello world")
    # A longer snapshot emits only the replacement tail.
    corrected = {
        "type": "full",
        "response": {"sections": [{"view_model": {"primitive": {"text": "Hello world!"}}}]},
    }
    assert meta._event_text([corrected], text) == ("!", "Hello world!")


def test_event_text_ignores_unrelated_patches():
    patch = {
        "type": "patch",
        "operations": [{"op": "delta", "path": "/other", "value": "ignored"}],
    }
    assert meta._event_text([patch], "") == ("", "")


def _walk_reply(frame):
    """Decode a 0x0d prompt frame back into its JSON envelope."""
    assert frame[0] == meta.META_WS_PROMPT_FRAME_TYPE
    length = frame[3] | (frame[4] << 8) | (frame[5] << 16)
    body = frame[6:]
    assert len(body) == length
    assert body[0] == 0 and body[1] == meta.META_WS_PROMPT_FRAME_FLAG
    return json.loads(body[2:])


def test_prompt_frame_patches_prompt_ids_and_conversation():
    template = meta.META_WS_HOME_TEMPLATE_B64
    conv_id = "c.0123456789ABCDEFGHI"
    frame = meta.build_ws_prompt_frame("hi there", conv_id, template, request_id="req-1")
    envelope = _walk_reply(frame)
    assert envelope["req-id"] == "req-1"

    fields = meta.parse_proto_fields(base64.b64decode(envelope["payload"]))
    message = meta._find_field(fields, 2)["value"]
    nested = meta.parse_proto_fields(message)
    assert meta._find_field(nested, 2)["value"] == b"hi there"

    top = meta._find_field(fields, 1)["value"]
    top_fields = meta.parse_proto_fields(top)
    assert meta._find_field(top_fields, 6)["value"] == b"req-1"
    branch = meta._find_field(top_fields, 10)["value"]
    assert meta._find_field(meta.parse_proto_fields(branch), 4)["value"] == conv_id.encode()


def test_intro_frame_header_and_payload():
    frame = meta.build_ws_intro_frame("c.abc")
    assert frame[0] == meta.META_WS_INTRO_FRAME_TYPE
    length = frame[3] | (frame[4] << 8) | (frame[5] << 16)
    assert length == len(frame) - 6
    payload = json.loads(frame[6:])
    assert payload["x-dgw-app-x-ecto-conversation-id"] == "c.abc"
    assert payload["x-dgw-app-client-payload-type"] == "PROTO_INSIDE_JSON"


def test_ws_url_carries_authorization_and_app_id():
    url = meta.build_ws_url("ecto1:abc", "req-1")
    assert url.startswith("wss://gateway.meta.ai/ws/clippy?")
    assert "Authorization=ecto1%3Aabc" in url
    assert "x-dgw-appid=1522763855472543" in url
    assert "x-dgw-app-clippy-request-id=req-1" in url


def test_stream_prompt_without_token_returns_none():
    class Creds:
        def auth_token(self):
            return None

    assert meta.stream_prompt(Creds(), "hi", session_state={}) is None


def test_delete_conversation_is_a_noop():
    assert meta.delete_conversation(object(), "c.abc") is None
