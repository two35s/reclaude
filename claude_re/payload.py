"""Fresh per-request payload builder."""
import copy
import uuid

from .config import DEFAULT_MODEL, PAYLOAD_TEMPLATE


def build_payload(prompt, model=DEFAULT_MODEL):
    """Build a fresh payload with new UUIDs."""
    p = copy.deepcopy(PAYLOAD_TEMPLATE)
    p["prompt"] = prompt
    p["model"] = model
    p["turn_message_uuids"]["human_message_uuid"] = str(uuid.uuid4())
    p["turn_message_uuids"]["assistant_message_uuid"] = str(uuid.uuid4())
    return p
