"""Meta AI credentials: cookie handling, challenge solving, token cache."""

import si.meta_creds as meta_creds
from si.meta_creds import LEGACY_SESSION_COOKIE, SESSION_COOKIE, MetaCredentials


class _Cookies:
    def __init__(self, values):
        self._values = values

    def get_dict(self):
        return dict(self._values)


class _Response:
    def __init__(self, status_code, text="", cookies=None):
        self.status_code = status_code
        self.text = text
        self.cookies = _Cookies(cookies or {})


def test_session_cookie_prefers_current_name_then_legacy():
    creds = MetaCredentials()
    creds.cookies = {LEGACY_SESSION_COOKIE: "old"}
    assert creds.session_cookie == "old"
    creds.cookies = {SESSION_COOKIE: "new", LEGACY_SESSION_COOKIE: "old"}
    assert creds.session_cookie == "new"


def test_cookie_header_skips_empty_values():
    creds = MetaCredentials()
    creds.cookies = {"ecto_1_sess": "abc", "empty": ""}
    assert creds.cookie_header() == "ecto_1_sess=abc"


def test_refresh_token_solves_challenge_and_caches(monkeypatch):
    meta_creds._TOKEN_CACHE.clear()
    calls = []
    challenge = "<script>fetch('/__rd_verify_test?challenge=3', {method:'POST'})</script>"
    app = '{"accessToken":"ecto1:fresh-token"}'

    class FakeSession:
        def __init__(self, *a, **k):
            pass

        def get(self, url, headers, timeout):
            calls.append(("get", headers.get("Cookie", "")))
            if len([c for c in calls if c[0] == "get"]) == 1:
                return _Response(403, challenge)
            return _Response(200, app)

        def post(self, url, headers, timeout):
            calls.append(("post", url))
            return _Response(200, cookies={"rd_challenge": "solved"})

    monkeypatch.setattr(meta_creds.cffi_requests, "Session", FakeSession)

    creds = MetaCredentials()
    creds.cookies = {SESSION_COOKIE: "abc"}
    assert creds.refresh_token() is True
    assert creds.access_token == "ecto1:fresh-token"
    assert creds.cookies["rd_challenge"] == "solved"
    assert ("post", "https://www.meta.ai/__rd_verify_test?challenge=3") in calls

    # Second call is served from the cache: no new HTTP request.
    before = len(calls)
    assert creds.refresh_token() is True
    assert len(calls) == before


def test_auth_token_ignores_a_stale_in_memory_token(monkeypatch):
    meta_creds._TOKEN_CACHE.clear()

    class FakeSession:
        def __init__(self, *a, **k):
            pass

        def get(self, url, headers, timeout):
            return _Response(200, '{"accessToken":"ecto1:fresh-token"}')

    monkeypatch.setattr(meta_creds.cffi_requests, "Session", FakeSession)
    creds = MetaCredentials()
    creds.cookies = {SESSION_COOKIE: "abc"}
    creds.access_token = "ecto1:stale"
    assert creds.auth_token() == "ecto1:fresh-token"


def test_save_and_load_do_not_persist_the_short_lived_token(monkeypatch, tmp_path):
    path = tmp_path / "meta_session.json"
    monkeypatch.setattr(meta_creds, "META_CRED_FILE", str(path))
    creds = MetaCredentials()
    creds.cookies = {SESSION_COOKIE: "abc"}
    creds.access_token = "ecto1:short-lived"
    creds.save()

    loaded = MetaCredentials()
    assert loaded.load() is True
    assert loaded.cookies == {SESSION_COOKIE: "abc"}
    assert loaded.access_token is None


def test_refresh_token_reports_missing_token(monkeypatch):
    meta_creds._TOKEN_CACHE.clear()

    class FakeSession:
        def __init__(self, *a, **k):
            pass

        def get(self, url, headers, timeout):
            return _Response(200, "<html>not logged in</html>")

    monkeypatch.setattr(meta_creds.cffi_requests, "Session", FakeSession)
    creds = MetaCredentials()
    creds.cookies = {SESSION_COOKIE: "abc"}
    assert creds.refresh_token() is False
    assert creds.access_token is None
