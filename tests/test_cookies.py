"""The 32-byte prefix decision follows Chrome's schema version."""

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
