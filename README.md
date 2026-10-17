# reclaude

Terminal client for Claude.ai — replays your own browser session against Claude's internal REST + SSE endpoint and streams replies token-by-token. No API key.

> Research tool. It runs under your Claude.ai account: its limits, tiers, and billing still apply. It does not bypass rate limits or paywalls.

## Install

```bash
git clone https://github.com/two35s/reclaude.git && cd reclaude
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m claude_re --opencode   # register the MCP tool with OpenCode (see below)
```

## Login (once)

```bash
python -m claude_re --from-chrome   # steal session from your logged-in Chrome (recommended)
python -m claude_re --auto-fetch    # or: launch Chrome, log in, auto-extract over CDP
python -m claude_re --manual        # or: paste org_id + Cookie header by hand
```

Saves to `~/.config/claude_re/claude_session.json` (`600`).

## Chat

```bash
python -m claude_re                 # REPL
python -m claude_re --tui           # full-screen chat
python -m claude_re --prompt "..."  # one shot
python -m claude_re --model haiku   # haiku | sonnet (default) | opus | fable …
```

TUI commands: `/new` `/model [name]` `/models` `/help` `/quit`.
REPL commands: same, plus `/stealth on|off` `/cleanup` `/clear-session`.

One server-side conversation per session gives multi-turn memory; stealth mode deletes it on exit so it never appears in your browser sidebar (`--no-stealth` to keep it).

## Proxy

```bash
python -m claude_re --proxy                 # OpenAI-compatible API on :8765
python -m claude_re --proxy --port 9000
python -m claude_re --proxy &               # run in the background
```

The proxy must be running for clients to see the models — if it exits, they disappear until you restart it. (Optional: a `KeepAlive: true` launchd agent keeps it up across reboots.)

Point [Crush](https://github.com/charmbracelet/crush), [OpenCode](https://opencode.ai), or any OpenAI client at `http://localhost:8765/v1`.

Streaming (`text/event-stream`) and non-streaming requests are both supported. Every streamed reply is also written to `~/.config/claude_re/last_response.txt` — handy when a client shows you nothing.

On a `429` the proxy returns the error instead of silently downgrading you to a weaker model — retries and model choice belong to the client (OpenCode retries on its own).

**Tool calling works.** OpenAI `tools` are emulated in text: the proxy appends the tool specs to the prompt, asks the model to emit fenced `tool_call` blocks, and converts them into real OpenAI `tool_calls` in the response. Send the results back as `tool` messages on the next request and the model continues. (Claude.ai's native tool channel keeps the stream open waiting for a result, which deadlocks clients that act only on finished responses — so the proxy doesn't use it.)

### Use as a normal OpenCode model

With the proxy running, `reclaude/sonnet`, `reclaude/haiku`, and `reclaude/opus` appear in OpenCode's `/models` picker like any other provider. Select one and chat normally — tools (read/edit/bash), streaming, and multi-turn all work through your Claude.ai account.

`python -m claude_re --opencode` prints the ready-to-paste `providers` block for your global config. Or paste it by hand:

```jsonc
{
  "providers": {
    "reclaude": {
      "name": "reclaude",
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": { "baseURL": "http://localhost:8765/v1" },
      "models": {
        "sonnet": { "name": "Claude Sonnet", "modelID": "reclaude/sonnet" },
        "haiku": { "name": "Claude Haiku", "modelID": "reclaude/haiku" },
        "opus": { "name": "Claude Opus", "modelID": "reclaude/opus" }
      }
    }
  }
}
```

## OpenCode MCP server

Expose reclaude to [OpenCode](https://opencode.ai) as a native tool (`claude_ask`) without making it your model provider — OpenCode keeps its own tool-capable model and can call Claude.ai through reclaude when you ask.

Install it with one command, run from this checkout:

```bash
python -m claude_re --opencode
```

That calls OpenCode's own `opencode mcp add`, so your existing config (comments included) is preserved. Restart OpenCode, then check `opencode mcp list`.

Your model can then call `claude_ask(prompt, model="sonnet")`; `model` accepts aliases (`sonnet`, `haiku`, `opus`) or a full model id.

<details>
<summary>Or add it by hand</summary>

```jsonc
{
  "mcp": {
    "servers": {
      "reclaude": {
        "type": "local",
        "command": ["/absolute/path/to/reclaude/.venv/bin/python", "-m", "claude_re.mcp_server"],
        "environment": { "PYTHONPATH": "/absolute/path/to/reclaude" }
      }
    }
  }
}
```

`PYTHONPATH` is what makes the module importable from any working directory, so no `cwd` is needed.

</details>

Check the handshake standalone:

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n' | .venv/bin/python -m claude_re.mcp_server
```

Notes:

- **Shared conversation.** All `claude_ask` calls reuse one Claude.ai conversation for the server's lifetime, so follow-ups keep context. It is **not** stealth-deleted and will appear in your Claude.ai sidebar until you remove it.
- **Diagnostics on stderr.** reclaude's progress/error output goes to stderr, so it never corrupts the MCP protocol stream on stdout.
- **Limitations.** Text-only passthrough — no image output, and no tool calls come back from Claude.ai. Requests run under your Claude.ai account, so plan limits apply. One call can take up to the HTTP timeout (120 s), longer under rate limiting.
- **Troubleshooting.** `Session invalid or expired` → re-run `python -m claude_re --auto-fetch`; the next `claude_ask` picks up the new credentials (no restart needed). If the tool is missing, check OpenCode's MCP logs and that the venv path in `command` is absolute and correct.

## More flags

`--batch FILE` one prompt per line → `~/.config/claude_re/batch_results.json` · `--jailbreak FILE` prompt from file · `--org-id/--cookie` inline creds · `--clear-session` wipe creds · `--login` save a Chrome profile without extracting · `--opencode` register the MCP tool with OpenCode and print the provider config.

## If it breaks

| Symptom | Fix |
|---|---|
| `Session invalid or expired` | re-run `--from-chrome` or `--auto-fetch` |
| `429` rate limited | wait, or switch model (`/model haiku` in the CLI/TUI) |
| Keychain prompt | click **Always Allow**, retry |

## Security

Your `sessionKey` is a password: never share, upload, or commit the credential file. `--clear-session` when done; if exposed, log out in the browser to invalidate it.

## Dev

```bash
pip install -r requirements-dev.txt
PYTHONPATH=. .venv/bin/python -m pytest -q   # tests
.venv/bin/python -m ruff check .             # lint
.venv/bin/python -m ruff format --check .    # formatting
```

## License

Research and educational use only. No warranty.
