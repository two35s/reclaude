"""Register reclaude with OpenCode.

The MCP server is registered through OpenCode's own CLI, which preserves the
rest of the user's config (comments included).  The OpenAI-compatible provider
entry has no CLI equivalent, so it is printed ready to paste.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys

from .compat import HAS_CFFI
from .config import CHATGPT_CRED_FILE, CHATGPT_DEFAULT_MODEL, DIR, MODEL_ALIASES

NAME = "reclaude"  # MCP server name, provider id, and model prefix
PICKER_MODELS = ("sonnet", "haiku", "opus")


def mcp_argv():
    """argv for `opencode mcp add`, pinning this interpreter and checkout.

    PYTHONPATH makes claude_re importable from any working directory, so the
    entry needs no `cwd` and no `pip install`.
    """
    return [
        "opencode",
        "mcp",
        "add",
        NAME,
        "--global",
        "--env",
        f"PYTHONPATH={DIR}",
        "--",
        sys.executable,
        "-m",
        "claude_re.mcp_server",
    ]


def global_config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "opencode", "opencode.jsonc")


def provider_block(port=8765):
    """The `providers` entry that exposes the running proxy as OpenCode models.

    The ChatGPT model is included only when chatgpt credentials are stored,
    since the proxy rejects ``chatgpt/`` requests without them.
    """
    models = {
        alias: {"name": f"Claude {alias.capitalize()}", "modelID": f"{NAME}/{alias}"}
        for alias in PICKER_MODELS
    }
    if os.path.exists(CHATGPT_CRED_FILE):
        models["chatgpt"] = {"name": "ChatGPT", "modelID": f"chatgpt/{CHATGPT_DEFAULT_MODEL}"}
    return {
        "providers": {
            NAME: {
                "name": NAME,
                "package": "@opencode/ai/providers/openai-compatible",
                "settings": {"baseURL": f"http://localhost:{port}/v1"},
                "models": models,
            }
        }
    }


def install(port=8765):
    """Register the MCP server globally, then print the optional provider config."""
    if not HAS_CFFI:
        print("[!] This Python has no HTTP backend — the server will not start.")
        print("    Re-run inside the venv: source .venv/bin/activate")
    argv = mcp_argv()
    if shutil.which("opencode") is None:
        print("[!] `opencode` is not on PATH. Run this to enable all projects:")
        print(f"    {shlex.join(argv)}")
    else:
        print("[*] Registering the MCP server for all projects...")
        if subprocess.run(argv).returncode != 0:
            print("[!] That failed — run it by hand to see the error:")
            print(f"    {shlex.join(argv)}")
    print("\n[+] Restart OpenCode, then check: opencode mcp list")
    print("[*] Optional — use Claude as a normal OpenCode model. Start the proxy")
    print(f"    (python -m claude_re --proxy) and paste into {global_config_path()}:\n")
    print(json.dumps(provider_block(port), indent=2))
    extra = ", ".join(a for a in MODEL_ALIASES if a not in PICKER_MODELS)
    print(f"\n    Other aliases you can add as models: {extra}")
