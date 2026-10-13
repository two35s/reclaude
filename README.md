# reclaude

A terminal client for Claude.ai. It replays your own browser session against Claude's internal REST + SSE endpoint and streams replies token-by-token — no API key.

> **Research tool.** Everything runs under your Claude.ai account, so its limits, tiers, and billing still apply. It replaces the browser UI with a terminal; it does not bypass rate limits or paywalls.

---

## Features

- **Two interfaces** — a line REPL, or `--tui` for a full-screen, opencode-style chat.
- **No API key** — reuses your existing Claude.ai session.
- **Live streaming** — SSE `content_block_delta` events print as they arrive.
- **Multi-turn memory** — one server-side conversation per terminal session.
- **Stealth cleanup** — optionally deletes that conversation on exit so it never shows in your browser sidebar.
- **Three ways to authenticate** — import an existing Chrome login, launch a capture browser, or paste cookies by hand.
- **OpenAI-compatible proxy** (`--proxy`) — expose your backend as a local `http://localhost:8765/v1` endpoint so [Crush](https://github.com/charmbracelet/crush) or any OpenAI-compatible client can use it.

---

## Crush Integration (OpenCode Successor)

Run reclaude as an OpenAI-compatible backend for **[Crush](https://github.com/charmbracelet/crush)** — the active terminal TUI descended from OpenCode:

```bash
# Terminal 1 — start the proxy
python -m claude_re --proxy          # listens on http://localhost:8765

# Terminal 2 — launch Crush
crush                                # models named "Claude * (reclaude)" appear in ctrl+l picker
```

`~/.config/crush/crushrc` is already configured by the integration setup. If you need to edit it:

```bash
# ~/.config/crush/crushrc
provider add reclaude --type openai --base-url "http://localhost:8765/v1"
model add reclaude/sonnet --name "Claude Sonnet (reclaude)" --context-window 200000
model add reclaude/haiku --name "Claude Haiku (reclaude)" --context-window 200000
```


## Requirements

- Python 3.9+
- Google Chrome — only for the credential-capture methods (`--from-chrome`, `--auto-fetch`, `--login`)

---

## Install

```bash
git clone <your-reclaude-url> && cd reclaude

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

The examples below assume the virtualenv is active. If you skip activation, call the interpreter directly, e.g. `.venv/bin/python -m claude_re`. On macOS/Linux there is usually no bare `python`; prefer the venv over the system `python3`.

---

## Quick start

```bash
# 1. Import the session from the Chrome you're already logged into
python -m claude_re --from-chrome

# 2. Chat
python -m claude_re
```

That's it. The first command writes `~/.config/claude_re/claude_session.json`; afterwards the plain command starts the interactive REPL.

---

## Getting credentials

Choose one method. All of them save the same credential file.

| Method | How it works | Platform |
|---|---|---|
| `--from-chrome` | Reads and decrypts cookies from a browser you're already logged into. **Recommended** — no second login. | macOS, Linux |
| `--auto-fetch` | Launches a dedicated Chrome profile, then extracts the session over local CDP. | macOS, Linux, Windows |
| `--manual` | You paste `org_id` and the `Cookie` header from DevTools. | Any |

### `--from-chrome`

```bash
python -m claude_re --from-chrome
```

Auto-discovers Chrome, Chromium, Brave, and Edge profiles. To target a specific one:

```bash
python -m claude_re --from-chrome "~/Library/Application Support/Google/Chrome/Default"
```

macOS will ask for the browser's `... Safe Storage` key from the Keychain — click **Always Allow** to avoid repeat prompts. Decryption uses the system `openssl` binary. On Windows, cookie values are DPAPI-protected; use `--auto-fetch` there instead.

### `--auto-fetch`

```bash
python -m claude_re --auto-fetch
```

A real Chrome window opens. Log in, open a chat, send one message, then return to the terminal and press ENTER. The session is extracted automatically over local CDP.

### `--manual`

```bash
python -m claude_re --manual
```

Follow the on-screen prompts: open a `completion` request in DevTools → Network, copy its `Cookie` header, and provide the `org_id` from its URL.

---

## Usage

```bash
python -m claude_re                                  # interactive REPL
python -m claude_re --tui                            # full-screen opencode-style chat
python -m claude_re --prompt "explain SSE"           # single prompt
python -m claude_re --model haiku                    # choose a model
python -m claude_re --no-stealth                     # keep chats in browser history
python -m claude_re --batch prompts.txt              # one prompt per line → batch_results.json
python -m claude_re --clear-session                  # wipe stored credentials
python -m claude_re --proxy                          # OpenAI-compatible proxy on :8765 (for Crush)
python -m claude_re --proxy --port 9000              # proxy on a custom port
python -m claude_re --stats                          # opencode usage dashboard for this project
python -m claude_re --stats --models --tools         # dashboard with model/tool breakdowns
```

### Flags

| Flag | Description |
|---|---|
| `--from-chrome [PROFILE]` | Import a session from an existing browser profile (auto-discovers; pass a path to narrow it). |
| `--auto-fetch` | Launch Chrome and extract the session over CDP. |
| `--login` | Open Chrome to log in and save the profile, without extracting. |
| `--manual` | Enter `org_id` / `Cookie` header by hand. |
| `--tui` | Full-screen chat interface (opencode-style), instead of the line REPL. |
| `--stats` | Usage dashboard for this project — delegates to opencode's own `stats` CLI (`opencode stats --project .`). Accepts `--days N`, `--year Y`, `--all`, `--models`, `--tools`, `--cost`, `--full`, `--limit N`, `--json`. |
| `--proxy` | Start an OpenAI-compatible HTTP proxy on `localhost:8765` for Crush/opencode integration. |
| `--port PORT` | Port for `--proxy` mode (default: `8765`). |
| `--prompt TEXT` | Send a single prompt, then exit. |
| `--model NAME` | Model alias or full id (default: `sonnet`). |
| `--batch FILE` | Send one prompt per line; writes `batch_results.json`. |
| `--jailbreak FILE` | Send the contents of a file as the prompt. |
| `--no-stealth` | Keep the server-side conversation after exit. |
| `--org-id` / `--cookie` | Supply credentials inline instead of a saved file. |
| `--clear-session` | Delete the stored credential file. |

### TUI keys

With `--tui`, a full-screen chat (like opencode's chat page):

| Key | Action |
|---|---|
| `enter` / `ctrl+s` | Send the message. |
| `ctrl+n` | New chat. |
| `ctrl+o` | Switch model (opens a picker; starts a new conversation). |
| `ctrl+a` | Switch chat / new chat. |
| `↑` `↓` `pgup` `pgdn` | Scroll the message stream. |
| `←` `→` `home` `end` | Move the input cursor. |
| `?` (empty input) / `F1` | Show keybinds. |
| `esc` | Clear input / close a dialog. |
| `ctrl+c` / `ctrl+d` | Quit (deletes conversations when stealth is on). |

### REPL commands

| Command | Effect |
|---|---|
| `/model <name>` | Switch model (starts a new conversation). |
| `/models` | List available models. |
| `/stealth on\|off` | Toggle conversation cleanup on exit. |
| `/new` | Start a fresh conversation. |
| `/cleanup` | Delete the session conversation now. |
| `/clear-session` | Wipe stored credentials. |
| `/help` | Show commands. |
| `exit` | Quit (auto-cleans when stealth is on). |

---

## Models

Aliases map to full model ids; any full id can also be passed directly.

| Shortcut | Model id | Availability |
|---|---|---|
| `haiku` | `claude-haiku-5-5` | free |
| `sonnet` | `claude-sonnet-5-5` | free — default |
| `sonnet-5` | `claude-sonnet-5` | free |
| `sonnet-4-6` | `claude-sonnet-4-6` | free |
| `haiku-4-5` | `claude-haiku-4-5` | free |
| `haiku-snap` | `claude-haiku-4-5-20251001` | free |
| `opus` | `claude-opus-5-5` | pro/max |
| `fable` | `claude-fable-5` | pro/specialized |

Availability depends on your plan, and the server is the final authority: an unsupported model returns a clear `Model '<id>' is not available on your account` message. Full ids that aren't aliased pass through unchanged.

On HTTP 429 the client falls back to Haiku once, then waits per `Retry-After` (with jitter, bounded retries).

---

## How it works

```
┌──────────┐     ┌───────────────┐     ┌──────────────┐
│ Terminal │────▶│  curl_cffi    │────▶│  claude.ai   │
│  You>    │     │  TLS + SSE    │     │ REST + SSE   │
│          │◀────│  stream       │◀────│              │
└──────────┘     └───────────────┘     └──────────────┘
```

1. **Capture** — credentials (`sessionKey`, `org_id`, cookies) are read from a browser session and saved.
2. **Replay** — prompts post to `/api/organizations/{org_id}/chat_conversations/{conv_id}/completion` with your cookies, using a Chrome TLS fingerprint matched to the declared user agent.
3. **Stream** — `content_block_delta` SSE events print live in the terminal.
4. **Memory** — one server-side conversation per session provides multi-turn context; stealth cleanup deletes it on exit.

---

## Project structure

```
reclaude/
├── claude_re/
│   ├── __main__.py        # python -m claude_re
│   ├── cli.py             # flags, modes, REPL loop
│   ├── compat.py          # curl_cffi / requests backends
│   ├── config.py          # paths, UA, models, payload template
│   ├── timezone.py        # IANA timezone detection
│   ├── creds.py           # CredentialManager
│   ├── cdp.py             # stdlib-only CDP client (no playwright)
│   ├── chrome.py          # launch / login helpers
│   ├── chrome_cookies.py  # read + decrypt an existing browser profile
│   ├── payload.py         # per-request payload builder
│   ├── http.py            # session reuse, retries, request backend
│   ├── streaming.py       # SSE parsing, rate-limit handling
│   ├── tui.py             # full-screen opencode-style chat (--tui)
│   ├── stats.py           # usage dashboard: delegates to `opencode stats --project .`
│   └── repl.py            # help + model listing
└── requirements.txt
```

Fingerprint and session reuse live in `http.py`; streaming and backoff live in `streaming.py`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `python: command not found` | Activate the venv or use `.venv/bin/python`. |
| `unrecognized arguments` | The `# ...` notes in docs are comments, not arguments. |
| Keychain prompt on `--from-chrome` | Click **Always Allow**; retry the command. |
| `Session invalid or expired` | Re-import with `--from-chrome` or `--auto-fetch`. |
| `429` / rate limited | Wait, or switch with `/model haiku`. |
| Cloudflare block | Re-run `--auto-fetch` to refresh cookies. |

---

## Security

- **Your `sessionKey` is a password.** Anyone holding the credential file (`~/.config/claude_re/claude_session.json`, `%APPDATA%\claude_re\` on Windows) has full access to your Claude.ai account. Never share, upload, or commit it.
- Credentials are stored as **plaintext JSON** with owner-only permissions (`600` on Unix). Run `--clear-session` when done, and log out in the browser to invalidate the session if it is exposed.
- The capture Chrome profile (`claude_profile/`) also holds a logged-in session — handle it the same way.
- `--from-chrome` reads your normal browser profile's cookie database and requests the browser's storage key from the OS (macOS Keychain; Linux `peanuts`). AES decryption shells out to the system `openssl`.
- The terminal prints the first ~25 characters of the session key during capture; avoid screen-sharing that step.

---

## License

Research and educational use only. No warranty. Use at your own risk.
