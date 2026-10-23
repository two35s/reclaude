"""OpenCode registration: MCP argv shape and the provider block."""

import sys

from si import opencode
from si.config import (
    CHATGPT_CRED_FILE,
    CHATGPT_DEFAULT_MODEL,
    DIR,
    META_CRED_FILE,
)


def test_mcp_argv_pins_interpreter_and_checkout():
    argv = opencode.mcp_argv()
    assert argv[:4] == ["opencode", "mcp", "add", "si"]
    assert "--global" in argv
    assert f"PYTHONPATH={DIR}" in argv
    # command after `--` is what OpenCode runs
    assert argv[argv.index("--") + 1 :] == [sys.executable, "-m", "si.mcp_server"]


def test_provider_block_matches_proxy_models(monkeypatch):
    # without stored chatgpt creds only the claude models are offered
    monkeypatch.setattr(opencode.os.path, "exists", lambda _: False)
    block = opencode.provider_block(port=9000)["providers"]["si"]
    assert block["settings"]["baseURL"] == "http://localhost:9000/v1"
    assert block["package"] == "@opencode/ai/providers/openai-compatible"
    assert set(block["models"]) == {"sonnet", "haiku", "opus"}
    assert block["models"]["sonnet"]["modelID"] == "si/sonnet"

    # with stored chatgpt creds the chatgpt model joins the block
    monkeypatch.setattr(opencode.os.path, "exists", lambda p: p == CHATGPT_CRED_FILE)
    block = opencode.provider_block(port=9000)["providers"]["si"]
    assert set(block["models"]) == {"sonnet", "haiku", "opus", "chatgpt"}
    assert block["models"]["chatgpt"]["modelID"] == f"chatgpt/{CHATGPT_DEFAULT_MODEL}"

    # and with stored meta creds the meta models join too
    monkeypatch.setattr(
        opencode.os.path, "exists", lambda p: p in (CHATGPT_CRED_FILE, META_CRED_FILE)
    )
    block = opencode.provider_block(port=9000)["providers"]["si"]
    assert set(block["models"]) == {
        "sonnet",
        "haiku",
        "opus",
        "chatgpt",
        "muse-spark",
        "muse-spark-thinking",
        "muse-spark-contemplating",
    }
    assert block["models"]["muse-spark"]["modelID"] == "si/muse-spark"


def test_install_without_opencode_prints_command_and_spawns_nothing(monkeypatch, capsys):
    def no_spawn(*a, **k):
        raise AssertionError("install() spawned a process without `opencode` on PATH")

    monkeypatch.setattr(opencode.shutil, "which", lambda _: None)
    monkeypatch.setattr(opencode.subprocess, "run", no_spawn)

    opencode.install()

    out = capsys.readouterr().out
    assert "opencode mcp add si" in out
    assert '"providers"' in out  # block is printed for pasting
