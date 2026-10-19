"""Credential persistence: atomic private saves, corrupt files, org choice."""

import json
import os

import pytest

from si.chatgpt_creds import SESSION_COOKIE, ChatGPTCredentials
from si.creds import CredentialManager
from si.jsonfile import write_private_json


def test_failed_write_leaves_the_old_file_intact(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text('{"ok": true}')
    with pytest.raises(TypeError):
        write_private_json(str(path), {"bad": object()})
    assert json.loads(path.read_text()) == {"ok": True}
    assert list(tmp_path.glob(".tmp-*")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_claude_save_replaces_a_world_readable_file(tmp_path, monkeypatch):
    path = tmp_path / "claude_session.json"
    path.write_text("{}")
    path.chmod(0o644)
    monkeypatch.setattr("si.creds.CRED_FILE", str(path))

    creds = CredentialManager()
    creds.org_id, creds.session_key, creds.cookies = "org", "key", {"sessionKey": "key"}
    creds.save()

    assert json.loads(path.read_text())["org_id"] == "org"
    assert (os.stat(path).st_mode & 0o777) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_chatgpt_save_replaces_a_world_readable_file(tmp_path, monkeypatch):
    path = tmp_path / "chatgpt_session.json"
    path.write_text("{}")
    path.chmod(0o644)
    monkeypatch.setattr("si.chatgpt_creds.CHATGPT_CRED_FILE", str(path))

    creds = ChatGPTCredentials()
    creds.cookies = {SESSION_COOKIE: "token"}
    creds.account_id = "acct"
    creds.save()

    assert json.loads(path.read_text())["account_id"] == "acct"
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_corrupt_credentials_load_as_false_not_a_crash(tmp_path, monkeypatch):
    claude = tmp_path / "claude_session.json"
    claude.write_text("{not json")
    monkeypatch.setattr("si.creds.CRED_FILE", str(claude))
    assert CredentialManager().load() is False

    chatgpt = tmp_path / "chatgpt_session.json"
    chatgpt.write_text("[not an object]")
    monkeypatch.setattr("si.chatgpt_creds.CHATGPT_CRED_FILE", str(chatgpt))
    assert ChatGPTCredentials().load() is False


def _auto_fetch(monkeypatch, cookies):
    import si.cdp as cdp_mod
    import si.chrome as chrome_mod

    monkeypatch.setattr(chrome_mod, "find_chrome", lambda: "/fake/chrome")
    monkeypatch.setattr(
        chrome_mod,
        "launch_real_chrome",
        lambda *a: (type("P", (), {"terminate": lambda s: None})(), 9222),
    )
    monkeypatch.setattr(cdp_mod, "extract_session", lambda port: cookies)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    monkeypatch.setattr(CredentialManager, "save", lambda self: None)
    return CredentialManager()


def test_auto_fetch_prefers_last_active_org(monkeypatch):
    import si.cdp as cdp_mod

    def fail(port):
        raise AssertionError("fetch_orgs must not run when lastActiveOrg exists")

    monkeypatch.setattr(cdp_mod, "fetch_orgs", fail)
    creds = _auto_fetch(monkeypatch, {"sessionKey": "key", "lastActiveOrg": "team-org"})
    assert creds.auto_fetch() is True
    assert creds.org_id == "team-org"


def test_auto_fetch_falls_back_to_the_first_org(monkeypatch):
    import si.cdp as cdp_mod

    monkeypatch.setattr(cdp_mod, "fetch_orgs", lambda port: [{"uuid": "only-org"}])
    creds = _auto_fetch(monkeypatch, {"sessionKey": "key"})
    assert creds.auto_fetch() is True
    assert creds.org_id == "only-org"
