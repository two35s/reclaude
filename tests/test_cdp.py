"""CDP client: URL validation, handshake, and websocket framing."""

import struct

import pytest

from si import cdp

_101 = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n"


class _FakeSock:
    """Socket double: replays a canned response, records what was sent."""

    def __init__(self, response=b""):
        self.response = response
        self.sent = b""

    def sendall(self, data):
        self.sent += data

    def recv(self, _size):
        chunk, self.response = self.response, b""
        return chunk


def _conn_with_sock(sock):
    conn = object.__new__(cdp.CDPConnection)  # skip the handshake
    conn._sock = sock
    conn._buf = b""
    return conn


def test_handshake_rejects_non_ws_urls():
    for url in ("wss://127.0.0.1:9222/devtools/page/1", "https://example.test/"):
        with pytest.raises(ValueError, match="only ws"):
            cdp.CDPConnection._handshake(url, timeout=1)


def test_handshake_accepts_loopback_and_sends_upgrade(monkeypatch):
    sock = _FakeSock(_101)
    monkeypatch.setattr(cdp.socket, "create_connection", lambda _addr, timeout: sock)

    out = cdp.CDPConnection._handshake("ws://127.0.0.1:9222/devtools/page/1", timeout=1)

    assert out is sock
    assert sock.sent.startswith(b"GET /devtools/page/1 HTTP/1.1\r\n")
    assert b"Upgrade: websocket" in sock.sent
    assert b"Sec-WebSocket-Key: " in sock.sent


def test_handshake_rejects_non_101_response(monkeypatch):
    sock = _FakeSock(b"HTTP/1.1 400 Bad Request\r\n\r\n")
    monkeypatch.setattr(cdp.socket, "create_connection", lambda _addr, timeout: sock)

    with pytest.raises(ConnectionError, match="handshake failed"):
        cdp.CDPConnection._handshake("ws://127.0.0.1:9222/devtools/page/1", timeout=1)


def test_handshake_rejects_closed_connection(monkeypatch):
    sock = _FakeSock(b"")
    monkeypatch.setattr(cdp.socket, "create_connection", lambda _addr, timeout: sock)

    with pytest.raises(ConnectionError, match="connection closed"):
        cdp.CDPConnection._handshake("ws://127.0.0.1:9222/devtools/page/1", timeout=1)


def test_send_text_sets_mask_bit_and_short_length():
    conn = _conn_with_sock(_FakeSock())

    conn._send_text("hi")

    frame = conn._sock.sent
    assert frame[:2] == b"\x81\x82"  # FIN+text, MASK+len(2)
    assert frame[2:6] == b"\x00\x00\x00\x00"
    assert frame[6:] == b"hi"


def test_send_text_uses_extended_length_encodings():
    conn = _conn_with_sock(_FakeSock())

    conn._send_text("x" * 200)
    assert conn._sock.sent[:4] == b"\x81\xfe" + struct.pack("!H", 200)

    conn._sock.sent = b""
    conn._send_text("x" * 70000)
    assert conn._sock.sent[:10] == b"\x81\xff" + struct.pack("!Q", 70000)


def test_recv_frame_unmasks_payload():
    mask = b"\x01\x02\x03\x04"
    payload = b"hello"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    raw = b"\x81" + bytes([0x80 | len(payload)]) + mask + masked
    conn = _conn_with_sock(_FakeSock(raw))

    fin, opcode, out = conn._recv_frame()

    assert (fin, opcode, out) == (True, 0x1, payload)
