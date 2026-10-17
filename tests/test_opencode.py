"""OpenCode registration: MCP argv shape and the provider block."""

import sys

from claude_re import opencode
from claude_re.config import DIR


def test_mcp_argv_pins_interpreter_and_checkout():
    argv = opencode.mcp_argv()
    assert argv[:4] == ["opencode", "mcp", "add", "reclaude"]
    assert "--global" in argv
    assert f"PYTHONPATH={DIR}" in argv
    # command after `--` is what OpenCode runs
    assert argv[argv.index("--") + 1 :] == [sys.executable, "-m", "claude_re.mcp_server"]


def test_provider_block_matches_proxy_models():
    block = opencode.provider_block(port=9000)["providers"]["reclaude"]
    assert block["settings"]["baseURL"] == "http://localhost:9000/v1"
    assert block["package"] == "@opencode/ai/providers/openai-compatible"
    assert set(block["models"]) == {"sonnet", "haiku", "opus"}
    assert block["models"]["sonnet"]["modelID"] == "reclaude/sonnet"


def test_install_without_opencode_prints_command_and_spawns_nothing(monkeypatch, capsys):
    def no_spawn(*a, **k):
        raise AssertionError("install() spawned a process without `opencode` on PATH")

    monkeypatch.setattr(opencode.shutil, "which", lambda _: None)
    monkeypatch.setattr(opencode.subprocess, "run", no_spawn)

    opencode.install()

    out = capsys.readouterr().out
    assert "opencode mcp add reclaude" in out
    assert '"providers"' in out  # block is printed for pasting
