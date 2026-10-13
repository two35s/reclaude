"""CLI entry point: arg parsing, modes, interactive REPL."""
import argparse
import json
import os
import time

from . import http as http_mod
from .chrome import do_login
from .compat import HAS_CFFI
from .config import CRED_FILE, DIR, SYSTEM_TIMEZONE, resolve_model
from .creds import CredentialManager
from .repl import print_help, print_models
from .streaming import stream_prompt

DEFAULT_BATCH_DELAY = 2


def main():
    print()
    print("  ╔════════════════════════════════════════════════════╗")
    print("  ║  Claude.ai RE Client v4 — Secure + Stealth        ║")
    print("  ╚════════════════════════════════════════════════════╝")
    print()

    ap = argparse.ArgumentParser(description="Claude.ai Security Research Client v4")
    ap.add_argument("--login",         action="store_true", help="Browser login (save profile)")
    ap.add_argument("--auto-fetch",    action="store_true", help="Auto-extract creds via Chrome+CDP")
    ap.add_argument("--from-chrome",   nargs="?", const="", default=None, metavar="PROFILE",
                    help="Extract creds from an already-logged-in Chrome profile")
    ap.add_argument("--manual",        action="store_true", help="Manual credential input")
    ap.add_argument("--prompt",        type=str, help="Single prompt to send")
    ap.add_argument("--model",         type=str, default="sonnet",
                    help="Model: haiku/sonnet/opus (default: sonnet)")
    ap.add_argument("--org-id",        type=str, help="Organization ID")
    ap.add_argument("--cookie",        type=str, help="Full Cookie header string")
    ap.add_argument("--jailbreak",     type=str, help="Read prompt from file")
    ap.add_argument("--batch",         type=str, help="Send multiple prompts from file")
    ap.add_argument("--no-stealth",    action="store_true", help="Disable stealth mode")
    ap.add_argument("--clear-session", action="store_true", help="Delete stored credentials")
    ap.add_argument("--tui",           action="store_true",
                    help="Simple full-screen chat")
    ap.add_argument("--proxy",         action="store_true",
                    help="Start an OpenAI-compatible proxy (for Crush/opencode integration)")
    ap.add_argument("--port",          type=int, default=8765,
                    help="Port for --proxy mode (default: 8765)")
    args = ap.parse_args()

    # ── Clear session ────────────────────────────────────
    if args.clear_session:
        creds = CredentialManager()
        creds.clear()
        return

    # ── Login ────────────────────────────────────────────
    if args.login:
        do_login()
        return

    # ── Credential resolution ────────────────────────────
    creds = CredentialManager()

    if args.auto_fetch:
        if not creds.auto_fetch():
            print("[!] Auto-fetch failed")
            return
    elif args.from_chrome is not None:
        if not creds.from_chrome(args.from_chrome or None):
            print("[!] Chrome extraction failed")
            return
    elif args.manual:
        creds.manual_input()
    elif args.org_id and args.cookie:
        creds.org_id = args.org_id
        creds._parse_cookies(args.cookie)
        creds.save()
    elif not creds.load():
        print("[!] No credentials. Use --manual, --auto-fetch, or --login")
        return

    if not creds.is_valid():
        print("[!] Invalid credentials")
        return

    # ── State ────────────────────────────────────────────
    model = resolve_model(args.model)
    stealth = not args.no_stealth

    # Simple full-screen chat.
    if args.tui:
        from .tui import run_tui
        run_tui(creds, model, stealth)
        return

    # OpenAI-compatible proxy mode (for Crush / archived opencode integration).
    if args.proxy:
        from .proxy import run_proxy
        run_proxy(creds, port=args.port)
        return

    # Session state: tracks the current conversation for memory
    # Created lazily on first prompt, deleted on exit if stealth
    session = {"conv_id": None, "created": False}

    def _cleanup_session():
        """Delete the session conversation if stealth is on."""
        if stealth and session.get("conv_id") and session.get("created"):
            print("\n[*] Cleaning up session conversation...")
            http_mod.delete_conversation(creds, session["conv_id"])
            print("[+] Session cleaned (invisible in browser)")
            session["conv_id"] = None
            session["created"] = False

    lib = "curl_cffi" if HAS_CFFI else "requests"
    mode = "stealth" if stealth else "visible"
    print(f"[+] model:   {model}")
    print(f"[+] tz:      {SYSTEM_TIMEZONE}")
    print(f"[+] http:    {lib}")
    print(f"[+] stealth: {mode}  (memory ON, cleanup on exit)")
    print(f"[+] creds:   {CRED_FILE}")
    print("[+] Type /help for commands\n")

    # ── Jailbreak mode ──────────────────────────────────
    if args.jailbreak:
        if not os.path.exists(args.jailbreak):
            print(f"[!] File not found: {args.jailbreak}")
            return
        with open(args.jailbreak, "r", encoding="utf-8") as f:
            prompt = f.read().strip()
        try:
            stream_prompt(creds, prompt, model, stealth, session)
        finally:
            _cleanup_session()
            http_mod.close_session()
        return

    # ── Batch mode ──────────────────────────────────────
    if args.batch:
        if not os.path.exists(args.batch):
            print(f"[!] File not found: {args.batch}")
            return
        with open(args.batch, "r", encoding="utf-8") as f:
            prompts = [line.strip() for line in f if line.strip()]
        results = []
        try:
            for i, p in enumerate(prompts):
                print(f"  [{i+1}/{len(prompts)}] {p[:60]}")
                r = stream_prompt(creds, p, model, stealth, session)
                results.append({"prompt": p, "response": r or ""})
                if i < len(prompts) - 1:
                    time.sleep(DEFAULT_BATCH_DELAY)
        finally:
            out = os.path.join(DIR, "batch_results.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            print(f"[+] Saved -> {out}")
            _cleanup_session()
            http_mod.close_session()
        return

    # ── Single prompt ───────────────────────────────────
    if args.prompt:
        try:
            stream_prompt(creds, args.prompt, model, stealth, session)
        finally:
            _cleanup_session()
            http_mod.close_session()
        return

    # ── Interactive REPL ────────────────────────────────
    try:
        while True:
            try:
                inp = input("  You> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not inp:
                continue
            if inp.lower() in ("exit", "quit", "q"):
                break

            # ── REPL commands ────────────────────────────
            if inp.startswith("/"):
                parts = inp.split(None, 1)
                cmd = parts[0].lower()
                arg = parts[1].strip() if len(parts) > 1 else ""

                if cmd == "/help":
                    print_help()
                elif cmd == "/models":
                    print_models(model)
                elif cmd == "/model":
                    if not arg:
                        print(f"  Current: {model}")
                        print("  Usage: /model haiku|sonnet|opus|sonnet-4-5")
                    else:
                        new = resolve_model(arg)
                        if new != model:
                            # Model change requires new conversation
                            _cleanup_session()
                            session["conv_id"] = None
                            session["created"] = False
                            model = new
                            print(f"  [+] Model -> {model} (new session)")
                        else:
                            print(f"  [+] Already on {model}")
                elif cmd == "/stealth":
                    if arg.lower() in ("on", "1", "true"):
                        stealth = True
                        print("  [+] Stealth ON (cleanup on exit)")
                    elif arg.lower() in ("off", "0", "false"):
                        stealth = False
                        print("  [+] Stealth OFF (conversations kept)")
                    else:
                        print(f"  Stealth: {'ON' if stealth else 'OFF'}")
                        print("  Usage: /stealth on|off")
                elif cmd == "/new":
                    _cleanup_session()
                    session["conv_id"] = None
                    session["created"] = False
                    print("  [+] Fresh conversation (next prompt starts new)")
                elif cmd == "/cleanup":
                    _cleanup_session()
                elif cmd == "/clear-session":
                    creds.clear()
                    print("  [!] Session wiped. Re-run --auto-fetch to continue.")
                    break
                else:
                    print(f"  [!] Unknown command: {cmd}. Try /help")
                continue

            # ── Send prompt ─────────────────────────────
            print()
            stream_prompt(creds, inp, model, stealth, session)
    finally:
        # Always cleanup on exit
        _cleanup_session()
        http_mod.close_session()
