"""Security hardening: handshake validation, cred-file perms, proxy gates."""

import http.client
import importlib
import json
import os
import threading
from http.server import HTTPServer

import pytest

import claude_re.proxy as proxy_mod
from claude_re.cdp import CDPConnection
from claude_re.cli import _inline_creds_conflict


def test_handshake_rejects_non_ws_url():
    with pytest.raises(ValueError):
        CDPConnection._handshake("http://127.0.0.1:9/nothing", timeout=1)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_load_tightens_world_readable_creds(tmp_path, monkeypatch):
    f = tmp_path / "claude_session.json"
    f.write_text(json.dumps({"org_id": "o", "session_key": "s", "cookies": {}}))
    f.chmod(0o644)
    monkeypatch.setattr("claude_re.creds.CRED_FILE", str(f))
    from claude_re.creds import CredentialManager

    assert CredentialManager().load() is True
    assert (os.stat(f).st_mode & 0o777) == 0o600


def test_inline_creds_refuse_org_mismatch(tmp_path, monkeypatch):
    f = tmp_path / "claude_session.json"
    f.write_text(json.dumps({"org_id": "stored-org", "session_key": "s", "cookies": {}}))
    f.chmod(0o600)
    monkeypatch.setattr("claude_re.creds.CRED_FILE", str(f))
    from claude_re.creds import CredentialManager

    assert _inline_creds_conflict(CredentialManager(), "other-org") is True
    assert _inline_creds_conflict(CredentialManager(), "stored-org") is False


def test_proxy_token_gate_and_body_cap(monkeypatch):
    monkeypatch.setenv("RECLAUDE_PROXY_TOKEN", "secret")
    mod = importlib.reload(proxy_mod)
    mod._Handler.creds = None
    server = HTTPServer(("127.0.0.1", 0), mod._Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/v1/models")
        assert conn.getresponse().status == 403

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/v1/models", headers={"Authorization": "Bearer secret"})
        assert conn.getresponse().status == 200

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/v1/chat/completions",
            body=b"x" * (mod.MAX_BODY_BYTES + 1),
            headers={"Authorization": "Bearer secret"},
        )
        assert conn.getresponse().status == 413
    finally:
        server.shutdown()
        server.server_close()
        monkeypatch.undo()
        importlib.reload(proxy_mod)
