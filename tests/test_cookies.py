"""The 32-byte prefix decision follows Chrome's schema version."""

from unittest import mock

from claude_re import chrome_cookies
from claude_re.config import AES_KEY_SIZE, CHROME_COOKIE_SCHEMA_VERSION_V24


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
