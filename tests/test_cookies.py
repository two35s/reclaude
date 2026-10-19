"""Cookie extraction: prefix handling, Linux key sources, value plausibility."""

import hashlib
import sqlite3
import subprocess
from unittest import mock

import pytest

from si import chrome_cookies
from si.config import AES_KEY_SIZE, CHROME_COOKIE_SCHEMA_VERSION_V24


def test_prefix_stripped_when_version_new():
    plaintext = b"\x00" * 32 + b"session"
    with mock.patch.object(chrome_cookies, "_aes_decrypt", return_value=plaintext + b"\x01"):
        value = chrome_cookies._decrypt(b"v10\x00", b"k" * AES_KEY_SIZE, strip_prefix=True)
    assert value == "session"


def test_prefix_kept_when_version_old():
    with mock.patch.object(chrome_cookies, "_aes_decrypt", return_value=b"session\x01"):
        value = chrome_cookies._decrypt(b"v10\x00", b"k" * AES_KEY_SIZE, strip_prefix=False)
    assert value == "session"


def test_version_constant():
    assert CHROME_COOKIE_SCHEMA_VERSION_V24 == 24


def _fake_openssl(calls, returncode=0):
    """subprocess.run stand-in: 'decrypts' by returning input unchanged."""

    def run(argv, input=None, **kw):
        calls.append((argv, input))
        return subprocess.CompletedProcess(argv, returncode, stdout=input or b"", stderr=b"")

    return run


def test_decrypt_batch_uses_one_subprocess_and_splits_by_input(monkeypatch):
    calls = []
    monkeypatch.setattr(chrome_cookies.subprocess, "run", _fake_openssl(calls))
    items = [("sessionKey", b"v10" + b"A" * 32), ("other", b"v10" + b"B" * 16)]
    out = chrome_cookies._decrypt_batch(items, b"k" * AES_KEY_SIZE, strip_prefix=False)
    assert len(calls) == 1  # one process for N cookies, not N processes
    assert out == {"sessionKey": "A" * 32, "other": "B" * 16}


def test_decrypt_batch_rejected_input_raises_for_per_value_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(chrome_cookies.subprocess, "run", _fake_openssl(calls, returncode=1))
    with pytest.raises(chrome_cookies.CookieExtractionError):
        chrome_cookies._decrypt_batch([("a", b"v10" + b"A" * 32)], b"k" * AES_KEY_SIZE, False)


def test_linux_v10_and_v11_use_different_secrets(monkeypatch):
    monkeypatch.setattr(chrome_cookies.sys, "platform", "linux")
    monkeypatch.setattr(chrome_cookies, "_keyring_secret", lambda info: b"keyring-secret")
    info = {
        "browser": "Chrome",
        "service": "Chrome Safe Storage",
        "account": "Chrome",
        "iterations": 1,
    }
    assert chrome_cookies._secret_for(info, b"v10") == b"peanuts"
    assert chrome_cookies._secret_for(info, b"v11") == b"keyring-secret"

    monkeypatch.setattr(chrome_cookies, "_keyring_secret", lambda info: None)
    assert chrome_cookies._secret_for(info, b"v11") is None


def test_keyring_secret_tries_service_then_application(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        out = "s3cret" if argv[1:3] == ["lookup", "application"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(chrome_cookies.shutil, "which", lambda name: "/usr/bin/secret-tool")
    monkeypatch.setattr(chrome_cookies.subprocess, "run", run)
    info = {"browser": "Chrome", "service": "Chrome Safe Storage", "account": "Chrome"}
    assert chrome_cookies._keyring_secret(info) == b"s3cret"
    assert len(calls) == 2  # classic service/account first, application fallback second


def test_wrong_key_output_is_not_cookie_text():
    assert chrome_cookies._cookie_text("sk-ant-sid01-abc123")
    assert not chrome_cookies._cookie_text("Vr\ufffd\ufffd\x10\x11")
    assert not chrome_cookies._cookie_text("vàlid")  # cookie values are ASCII


def _write_cookies_db(profile, rows, version=CHROME_COOKIE_SCHEMA_VERSION_V24):
    """Create a minimal Chrome Cookies DB (schema >= 24) under a profile dir."""
    profile.mkdir(parents=True)
    con = sqlite3.connect(profile / "Cookies")
    con.execute("CREATE TABLE cookies (name TEXT, host_key TEXT, encrypted_value BLOB, value TEXT)")
    con.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    con.execute("INSERT INTO meta VALUES ('version', ?)", (str(version),))
    con.executemany(
        "INSERT INTO cookies VALUES (?, '.claude.ai', ?, NULL)",
        rows,
    )
    con.commit()
    con.close()


def test_v11_profile_uses_keyring_secret_end_to_end(tmp_path, monkeypatch):
    """Reporter's setup: v11 cookies + desktop keyring -> correct values."""
    monkeypatch.setattr(chrome_cookies.sys, "platform", "linux")
    monkeypatch.setattr(chrome_cookies, "_keyring_secret", lambda info: b"keyring-secret")
    profile = tmp_path / "Default"
    _write_cookies_db(
        profile,
        [
            ("sessionKey", b"v11session"),
            ("lastActiveOrg", b"v11org"),
        ],
    )
    prefix = b"\x00" * 32  # schema >= 24 prepends SHA256(host_key)
    plain = {
        b"session": prefix + b"sk-ant-sid01-abc123",
        b"org": prefix + b"6f5c1e9a-0000-4000-8000-000000000000",
    }
    seen = {}

    def decrypt_many(ciphertexts, key):
        seen["key"] = key
        return [plain[ct] for ct in ciphertexts]

    monkeypatch.setattr(chrome_cookies, "_aes_decrypt_many", decrypt_many)
    cookies = chrome_cookies.read_site_cookies("claude", str(profile))
    assert cookies == {
        "sessionKey": "sk-ant-sid01-abc123",
        "lastActiveOrg": "6f5c1e9a-0000-4000-8000-000000000000",
    }
    assert seen["key"] == hashlib.pbkdf2_hmac(
        "sha1", b"keyring-secret", b"saltysalt", 1, AES_KEY_SIZE
    )


def test_v11_garbage_is_refused_not_returned(tmp_path, monkeypatch):
    """Wrong-key noise must never become claude_session.json (issue #13)."""
    monkeypatch.setattr(chrome_cookies.sys, "platform", "linux")
    monkeypatch.setattr(chrome_cookies, "_keyring_secret", lambda info: b"wrong-secret")
    profile = tmp_path / "Default"
    _write_cookies_db(
        profile,
        [
            ("sessionKey", b"v11session"),
            ("lastActiveOrg", b"v11org"),
        ],
    )
    monkeypatch.setattr(
        chrome_cookies, "_aes_decrypt_many", lambda cts, key: [b"\x9c\xff" * 40 for _ in cts]
    )
    with pytest.raises(chrome_cookies.CookieExtractionError) as exc:
        chrome_cookies.read_site_cookies("claude", str(profile))
    assert "did not decrypt to text" in str(exc.value)


def test_v11_without_keyring_refuses_with_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(chrome_cookies.sys, "platform", "linux")
    monkeypatch.setattr(chrome_cookies, "_keyring_secret", lambda info: None)
    profile = tmp_path / "Default"
    _write_cookies_db(profile, [("sessionKey", b"v11session")])
    with pytest.raises(chrome_cookies.CookieExtractionError) as exc:
        chrome_cookies.read_site_cookies("claude", str(profile))
    assert "libsecret-tools" in str(exc.value)
