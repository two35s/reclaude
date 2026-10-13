# reclaude

Terminal client for Claude.ai — replays your own browser session against Claude's internal REST + SSE endpoint and streams replies token-by-token. No API key.

> Research tool. It runs under your Claude.ai account: its limits, tiers, and billing still apply. It does not bypass rate limits or paywalls.

## Install

```bash
git clone <url> && cd reclaude
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
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
```

Point [Crush](https://github.com/charmbracelet/crush) or any OpenAI client at `http://localhost:8765/v1`.

## More flags

`--batch FILE` one prompt per line → `batch_results.json` · `--jailbreak FILE` prompt from file · `--org-id/--cookie` inline creds · `--clear-session` wipe creds · `--login` save a Chrome profile without extracting.

## If it breaks

| Symptom | Fix |
|---|---|
| `Session invalid or expired` | re-run `--from-chrome` or `--auto-fetch` |
| `429` rate limited | wait, or `/model haiku` |
| Keychain prompt | click **Always Allow**, retry |

## Security

Your `sessionKey` is a password: never share, upload, or commit the credential file. `--clear-session` when done; if exposed, log out in the browser to invalidate it.

## License

Research and educational use only. No warranty.
