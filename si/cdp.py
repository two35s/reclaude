"""Minimal stdlib CDP client (replaces playwright for credential extraction).

Speaks just enough Chrome DevTools Protocol over a raw WebSocket to:
  - list open targets (/json/list)
  - read cookies (Storage.getCookies)
  - run JS in the page (Runtime.evaluate, awaited)

No third-party dependencies: socket + ssl + hashlib + json only.
"""

import base64
import hashlib
import json
import socket
import struct
import urllib.request

# CDP constants
CDP_HTTP_TIMEOUT = 10
CDP_WEBSOCKET_TIMEOUT = 15
CDP_EVALUATE_TIMEOUT = 20
WEBSOCKET_RECEIVE_BUFFER = 65536


def _http_get_json(url, timeout=CDP_HTTP_TIMEOUT):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def list_targets(cdp_port, timeout=CDP_HTTP_TIMEOUT):
    """Return DevTools /json/list targets."""
    return _http_get_json(f"http://127.0.0.1:{cdp_port}/json/list", timeout)


class CDPConnection:
    """Tiny blocking CDP client over one websocket."""

    def __init__(self, ws_url, timeout=CDP_WEBSOCKET_TIMEOUT):
        self._sock = self._handshake(ws_url, timeout)
        self._next_id = 0
        self._buf = b""

    # ── websocket handshake + framing ────────────────────

    @staticmethod
    def _handshake(ws_url, timeout):
        if not ws_url.startswith("ws://"):
            raise ValueError("only ws:// loopback supported")
        host_port, _, path = ws_url[5:].partition("/")
        host, _, port = host_port.partition(":")
        port = int(port or 80)
        key = base64.b64encode(hashlib.sha1(str(port).encode()).digest()[:16]).decode()
        sock = socket.create_connection((host, port), timeout=timeout)
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP handshake: connection closed")
            resp += chunk
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"CDP handshake failed: {resp[:120]!r}")
        return sock

    def _send_text(self, text):
        data = text.encode("utf-8")
        # Client frames MUST be masked. Mask key: 4 fixed bytes (fine on loopback).
        mask = b"\x00\x00\x00\x00"
        header = bytes([0x81])
        n = len(data)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        # XOR with zero mask = identity, but still a valid masked frame.
        self._sock.sendall(header + mask + data)

    def _recv_frame(self):
        while len(self._buf) < 2:
            chunk = self._sock.recv(WEBSOCKET_RECEIVE_BUFFER)
            if not chunk:
                raise ConnectionError("CDP socket closed")
            self._buf += chunk
        b1, b2 = self._buf[0], self._buf[1]
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        length = b2 & 0x7F
        idx = 2
        if length == 126:
            while len(self._buf) < idx + 2:
                self._buf += self._sock.recv(WEBSOCKET_RECEIVE_BUFFER)
            length = struct.unpack("!H", self._buf[idx : idx + 2])[0]
            idx += 2
        elif length == 127:
            while len(self._buf) < idx + 8:
                self._buf += self._sock.recv(WEBSOCKET_RECEIVE_BUFFER)
            length = struct.unpack("!Q", self._buf[idx : idx + 8])[0]
            idx += 8
        if masked:
            while len(self._buf) < idx + 4:
                self._buf += self._sock.recv(WEBSOCKET_RECEIVE_BUFFER)
            mask = self._buf[idx : idx + 4]
            idx += 4
        else:
            mask = None
        while len(self._buf) < idx + length:
            chunk = self._sock.recv(WEBSOCKET_RECEIVE_BUFFER)
            if not chunk:
                raise ConnectionError("CDP socket closed mid-frame")
            self._buf += chunk
        payload = self._buf[idx : idx + length]
        self._buf = self._buf[idx + length :]
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return bool(b1 & 0x80), opcode, payload

    def _recv_text(self):
        # A message may be split across frames (opcode 0x1 starts, 0x0
        # continues); keep reading until the frame with FIN so large
        # responses (e.g. Storage.getCookies) are not truncated.
        parts = []
        while True:
            fin, opcode, payload = self._recv_frame()
            if opcode == 0x8:  # close
                raise ConnectionError("CDP websocket closed by Chrome")
            if opcode == 0x9:  # ping → pong
                self._sock.sendall(b"\x8a\x00")
                continue
            if opcode == 0xA:  # pong — ignore
                continue
            if opcode in (0x1, 0x0):  # text / continuation
                parts.append(payload)
                if fin:
                    break
        return b"".join(parts).decode("utf-8")

    # ── CDP rpc ──────────────────────────────────────────

    def call(self, method, params=None, timeout=CDP_WEBSOCKET_TIMEOUT):
        self._next_id += 1
        msg_id = self._next_id
        self._send_text(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        self._sock.settimeout(timeout)
        try:
            while True:
                try:
                    text = self._recv_text()
                except TimeoutError:
                    raise TimeoutError(f"CDP {method}: no response")
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") == msg_id:
                    if "error" in msg:
                        raise RuntimeError(f"CDP {method}: {msg['error']}")
                    return msg.get("result", {})
                # else: async event (Target.attachedToTarget etc.) — ignore
        finally:
            self._sock.settimeout(None)

    def evaluate(self, expression, timeout=CDP_EVALUATE_TIMEOUT):
        """Run JS in the page, awaiting promises. Returns the remote value."""
        res = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
            },
            timeout=timeout,
        )
        remote = res.get("result", {})
        if remote.get("subtype") == "error":
            raise RuntimeError(f"CDP evaluate: {remote.get('description', 'js error')}")
        return remote.get("value")

    def close(self):
        try:
            self._sock.sendall(b"\x88\x00")
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


def extract_session(cdp_port):
    """Pull claude.ai cookies from the first claude.ai page target.

    Returns a {name: value} dict. Raises on failure.
    """
    targets = list_targets(cdp_port)
    page = None
    for t in targets:
        if t.get("type") != "page":
            continue
        if "claude.ai" in (t.get("url") or ""):
            page = t
            break
    if page is None:
        # Fall back to any page target (user may be on the login screen).
        for t in targets:
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                page = t
                break
    if page is None or not page.get("webSocketDebuggerUrl"):
        raise RuntimeError(
            "no debuggable page target found — open claude.ai in the launched Chrome"
        )

    conn = CDPConnection(page["webSocketDebuggerUrl"])
    try:
        cookies = {}
        try:
            res = conn.call("Storage.getCookies")
            for c in res.get("cookies", []):
                if "claude.ai" in c.get("domain", ""):
                    cookies[c["name"]] = c["value"]
        except RuntimeError:
            # Older builds: fall back to Network domain.
            conn.call("Network.enable")
            res = conn.call("Network.getAllCookies")
            for c in res.get("cookies", []):
                if "claude.ai" in c.get("domain", ""):
                    cookies[c["name"]] = c["value"]
        return cookies
    finally:
        conn.close()


def fetch_orgs(cdp_port):
    """Return parsed /api/organizations via in-page fetch. Returns list or None."""
    targets = list_targets(cdp_port)
    for t in targets:
        if t.get("type") != "page" or "claude.ai" not in (t.get("url") or ""):
            continue
        if not t.get("webSocketDebuggerUrl"):
            continue
        conn = CDPConnection(t["webSocketDebuggerUrl"])
        try:
            return conn.evaluate(
                "(async () => { try { const r = await fetch('/api/organizations');"
                " return await r.json(); } catch(e) { return null; } })()"
            )
        except Exception:
            continue
        finally:
            conn.close()
    return None
