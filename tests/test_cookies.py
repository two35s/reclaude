"""Cookie extraction: prefix handling, Linux key sources, value plausibility."""

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
