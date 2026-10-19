"""Security hardening: handshake validation, cred-file perms, proxy gates."""

import http.client
import importlib
import json
import os
import threading
from http.server import HTTPServer

import pytest

import si.proxy as proxy_mod
from si.cdp import CDPConnection
from si.cli import _inline_creds_conflict


def test_handshake_rejects_non_ws_url():
    with pytest.raises(ValueError):
        CDPConnection._handshake("http://127.0.0.1:9/nothing", timeout=1)


def test_recv_text_reassembles_fragmented_frames():
    # A CDP message split across frames: 0x1 starts (FIN=0), 0x0 continues
    # (FIN=1). Reading only the first frame would truncate the JSON.
    part1, part2 = b'{"id":1,', b'"result":{}}'
    wire = bytes([0x01, len(part1)]) + part1 + bytes([0x80, len(part2)]) + part2

    class _FakeSock:
        def __init__(self, data):
            self._data = data

        def recv(self, n):
            chunk, self._data = self._data[:n], self._data[n:]
            return chunk

    conn = CDPConnection.__new__(CDPConnection)
    conn._buf = b""
    conn._sock = _FakeSock(wire)
    assert conn._recv_text() == '{"id":1,"result":{}}'


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_load_tightens_world_readable_creds(tmp_path, monkeypatch):
    f = tmp_path / "claude_session.json"
    f.write_text(json.dumps({"org_id": "o", "session_key": "s", "cookies": {}}))
    f.chmod(0o644)
    monkeypatch.setattr("si.creds.CRED_FILE", str(f))
    from si.creds import CredentialManager

    assert CredentialManager().load() is True
    assert (os.stat(f).st_mode & 0o777) == 0o600


def test_inline_creds_refuse_org_mismatch(tmp_path, monkeypatch):
    f = tmp_path / "claude_session.json"
    f.write_text(json.dumps({"org_id": "stored-org", "session_key": "s", "cookies": {}}))
    f.chmod(0o600)
    monkeypatch.setattr("si.creds.CRED_FILE", str(f))
    from si.creds import CredentialManager

    assert _inline_creds_conflict(CredentialManager(), "other-org") is True
    assert _inline_creds_conflict(CredentialManager(), "stored-org") is False


def test_proxy_token_gate_and_body_cap(monkeypatch):
    monkeypatch.setenv("SI_PROXY_TOKEN", "secret")
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
