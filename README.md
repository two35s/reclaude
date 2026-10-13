# reclaude

Terminal client for Claude.ai — no API key. Replays your own browser session against Claude's internal REST + SSE endpoint and streams replies word-by-word.

> **Research tool.** Everything runs under your Claude.ai account: its limits, tiers, and billing still apply. This does not bypass rate limits or paywalls — it only replaces the browser UI with a terminal.

---

## How it works

```
┌──────────┐     ┌───────────────┐     ┌──────────────┐
│ Terminal  │────▶│  curl_cffi    │────▶│ claude.ai    │
│ (You>)   │     │  TLS + SSE    │     │ REST + SSE   │
│          │◀────│  stream       │◀────│              │
└──────────┘     └───────────────┘     └──────────────┘
```

1. **Capture** — real Chrome launches, you log in and send one chat message, credentials (`sessionKey`, `org_id`, cookies) are extracted over local CDP and saved.
2. **Replay** — prompts go to `/api/organizations/{org_id}/chat_conversations/{conv_id}/completion` with your cookies, using a Chrome TLS fingerprint matched to the declared user agent.
3. **Stream** — `content_block_delta` SSE events print live in the terminal.
4. **Memory** — one server-side conversation per session gives multi-turn context; optional cleanup deletes it on exit.

---

## Install

```bash
git clone <your-reclaude-url> && cd reclaude
pip install -r requirements.txt
```

Requires Python 3.9+ and Google Chrome (only for the one-time credential capture).

---

## Usage

### 1. Get credentials (one-time)

```bash
# Recommended: auto-extract via real Chrome + CDP
python -m claude_re --auto-fetch
# 1. Chrome opens → log in, open a chat, send a message
# 2. Press ENTER in the terminal, paste the chat URL when asked

# Alternative: paste values from DevTools → Network → 'completion' request
python -m claude_re --manual

# Simplest: reuse a browser you're already logged into (no second login)
python -m claude_re --from-chrome
# auto-discovers Chrome/Chromium/Brave/Edge on macOS/Linux; pass a path to
# pick a specific profile:
python -m claude_re --from-chrome "~/Library/Application Support/Google/Chrome/Default"
```

### 2. Chat

```bash
python -m claude_re                          # interactive REPL
python -m claude_re --prompt "explain SSE"   # single prompt
python -m claude_re --model haiku            # pick model upfront
python -m claude_re --no-stealth             # keep chats in browser history
python -m claude_re --models-refresh         # refresh model list first
python -m claude_re --batch prompts.txt      # one prompt per line → batch_results.json
python -m claude_re --clear-session          # wipe stored credentials
```

### REPL commands

| Command | Effect |
|---|---|
| `/model <name>` | Switch model (new conversation) |
| `/models` | List models (cached server discovery + static) |
| `/models refresh` | Force-refresh model list from server |
| `/stealth on\|off` | Toggle conversation cleanup on exit |
| `/new` | Start a fresh conversation |
| `/cleanup` | Delete the session conversation now |
| `/clear-session` | Wipe stored credentials |
| `/help` | Show commands |
| `exit` | Quit (auto-cleans if stealth is on) |

### Models

| Shortcut | Full name | Tier |
|---|---|---|
| `haiku` | `claude-haiku-5-5` (default cheap) | free |
| `sonnet` | `claude-sonnet-5-5` (default) | free |
| `sonnet-5` | `claude-sonnet-5` | free |
| `haiku-4-5` | `claude-haiku-4-5` | free |
| `sonnet-4-6` | `claude-sonnet-4-6` | free |
| `sonnet-4-5` | `claude-sonnet-4-5` | free |
| `haiku-snap` | `claude-haiku-4-5-20251001` | free |
| `sonnet-snap` | `claude-sonnet-4-5-20250929` | free |
| `opus` | `claude-opus-5-5` | pro/max |
| `opus-5` | `claude-opus-5` | pro/max |
| `opus-4-8` | `claude-opus-4-8` | pro/max |
| `opus-4-7` | `claude-opus-4-7` | pro/max |
| `opus-4-6` | `claude-opus-4-6` | pro/max |
| `opus-3` | `claude-3-opus-20240229` | pro/max |
| `fable` | `claude-fable-5` | pro/specialized |
| `mythos` | `claude-mythos-5` | pro/specialized |

Unknown names pass through as-is. On 429 the client falls back to Haiku once, then waits per `Retry-After` (with jitter, bounded retries).

---

## Project structure

```
reclaude/
├── claude_re/
│   ├── __main__.py   # python -m claude_re
│   ├── cli.py        # flags, modes, REPL loop
│   ├── compat.py     # curl_cffi / requests backends
│   ├── config.py     # paths, UA, models, payload template
│   ├── timezone.py   # IANA timezone detection
│   ├── creds.py      # CredentialManager
│   ├── cdp.py        # stdlib-only CDP client (no playwright needed)
│   ├── chrome.py     # real-Chrome launch / login
│   ├── chrome_cookies.py # read/decrypt session from an existing Chrome profile
│   ├── payload.py    # per-request payload builder
│   ├── http.py       # session reuse, retries, model discovery
│   ├── streaming.py  # SSE parsing, rate-limit handling
│   └── repl.py       # help + model listing
└── requirements.txt
```

Protocol details live where they belong: fingerprint + session reuse in `http.py`, streaming and backoff in `streaming.py`.

---

## Security warnings

- **Your `sessionKey` is a password.** Anyone holding the credential file (`~/.config/claude_re/claude_session.json`, `%APPDATA%\claude_re\` on Windows) has full access to your Claude.ai account. Never share, upload, or commit it.
- Credentials are stored as **plaintext JSON** (owner-only `600` on Unix). Run `--clear-session` when done, and log out in the browser to invalidate the session if exposed.
- The Chrome profile (`claude_profile/`) also holds a logged-in session — same handling applies.
- `--from-chrome` reads your normal browser profile's cookie DB and asks the OS for the browser's storage key (macOS Keychain; Linux `peanuts`). Click **Always Allow** to avoid repeat prompts; AES decryption shells out to the system `openssl`.
- The terminal prints the first ~25 chars of the session key during `--auto-fetch`; avoid screen-sharing that step.

---

## License

Research and educational use only. No warranty. Use at your own risk.
