"""HTTP layer: retry policy and session teardown."""

import si.http as http


class _Resp:
    def __init__(self, status_code, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


def test_fetch_with_retries_retries_5xx_then_returns_success(monkeypatch):
    responses = [_Resp(500), _Resp(200)]
    calls = []

    def fake_request(*args, **kwargs):
        calls.append(1)
        return responses.pop(0)

    monkeypatch.setattr(http, "request", fake_request)
    monkeypatch.setattr(http.time, "sleep", lambda _s: None)

    resp = http.fetch_with_retries("GET", "https://example.test/api", max_retries=3)
    assert resp.status_code == 200
    assert len(calls) == 2


def test_fetch_with_retries_honors_retry_after(monkeypatch):
    responses = [_Resp(429, {"Retry-After": "7"}), _Resp(200)]
    sleeps = []

    monkeypatch.setattr(http, "request", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr(http.time, "sleep", sleeps.append)

    resp = http.fetch_with_retries("POST", "https://example.test/api", max_retries=1)
    assert resp.status_code == 200
    assert sleeps and sleeps[0] >= 7


def test_fetch_with_retries_returns_none_after_network_errors(monkeypatch):
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise ConnectionError("down")

    monkeypatch.setattr(http, "request", boom)
    monkeypatch.setattr(http.time, "sleep", lambda _s: None)

    assert http.fetch_with_retries("GET", "https://example.test/api", max_retries=2) is None
    assert len(calls) == 3  # initial attempt + max_retries


def test_fetch_with_retries_does_not_retry_4xx(monkeypatch):
    responses = [_Resp(401)]

    monkeypatch.setattr(http, "request", lambda *a, **k: responses.pop(0))

    resp = http.fetch_with_retries("GET", "https://example.test/api", max_retries=3)
    assert resp.status_code == 401
    assert responses == []  # the 401 was the only response consumed


def test_close_session_closes_and_resets_shared_session(monkeypatch):
    class _Session:
        closed = False

        def close(self):
            self.closed = True

    sess = _Session()
    monkeypatch.setattr(http, "_HTTP_SESSION", sess)

    http.close_session()

    assert sess.closed
    assert http._HTTP_SESSION is None
