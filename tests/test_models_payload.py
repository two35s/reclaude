"""Model aliases pass through unknown names; payloads get fresh UUIDs."""

from si.config import MODEL_ALIASES, PAYLOAD_TEMPLATE, resolve_model
from si.payload import build_payload


def test_resolve_alias_and_passthrough():
    assert resolve_model("sonnet") == MODEL_ALIASES["sonnet"]
    assert resolve_model("claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_unique_uuids_and_template_untouched():
    a, b = build_payload("hi"), build_payload("hi")
    au, bu = a["turn_message_uuids"], b["turn_message_uuids"]
    assert au["human_message_uuid"] != au["assistant_message_uuid"]
    assert au["human_message_uuid"] != bu["human_message_uuid"]
    assert a["prompt"] == "hi"
    assert PAYLOAD_TEMPLATE["prompt"] == ""
