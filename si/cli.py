"""CLI entry point: arg parsing, modes, interactive REPL."""

import argparse
import json
import os
import time

from . import http as http_mod
from .chrome import do_login
from .config import _CONFIG_DIR, CRED_FILE, SYSTEM_TIMEZONE, resolve_model
from .creds import CredentialManager
from .repl import print_help, print_models
from .streaming import stream_prompt


def _inline_creds_conflict(creds, org_id):
    """True when stored valid creds belong to a different org (refuse overwrite)."""
    return bool(creds.load() and creds.is_valid() and creds.org_id != org_id)


DEFAULT_BATCH_DELAY = 2


def _run_proxy(creds, port):
    """Start the proxy with every provider whose credentials are stored."""
    from .chatgpt_creds import ChatGPTCredentials
    from .meta_creds import MetaCredentials
    from .proxy import run_proxy

    cg = ChatGPTCredentials()
    cg = cg if cg.load() else None
    mc = MetaCredentials()
    mc = mc if mc.load() else None
    run_proxy(creds, port=port, chatgpt_creds=cg, meta_creds=mc)


def main():
    print()
    print("  ╔════════════════════════════════════════════════════╗")
    print("  ║  SI — Secure + Stealth                             ║")
    print("  ╚════════════════════════════════════════════════════╝")
    print()

    ap = argparse.ArgumentParser(description="Claude.ai Security Research Client v4")
    ap.add_argument("--login", action="store_true", help="Browser login (save profile)")
    ap.add_argument("--auto-fetch", action="store_true", help="Auto-extract creds via Chrome+CDP")
    ap.add_argument(
        "--from-chrome",
        nargs="?",
        const="",
        default=None,
        metavar="PROFILE",
        help="Extract creds from an already-logged-in Chrome profile",
    )
    ap.add_argument("--manual", action="store_true", help="Manual credential input")
    ap.add_argument(
        "--chatgpt",
        action="store_true",
        help="With --from-chrome/--manual: store ChatGPT credentials instead of Claude",
    )
    ap.add_argument(
        "--meta",
        action="store_true",
        help="With --from-chrome/--manual: store Meta AI credentials instead of Claude",
    )
    ap.add_argument("--prompt", type=str, help="Single prompt to send")
    ap.add_argument(
        "--model", type=str, default="sonnet", help="Model: haiku/sonnet/opus (default: sonnet)"
    )
    ap.add_argument("--org-id", type=str, help="Organization ID")
    ap.add_argument("--cookie", type=str, help="Full Cookie header string")
    ap.add_argument("--jailbreak", type=str, help="Read prompt from file")
    ap.add_argument("--batch", type=str, help="Send multiple prompts from file")
    ap.add_argument("--no-stealth", action="store_true", help="Disable stealth mode")
    ap.add_argument("--clear-session", action="store_true", help="Delete stored credentials")
    ap.add_argument("--tui", action="store_true", help="Simple full-screen chat")
    ap.add_argument(
        "--proxy",
        action="store_true",
        help="Start an OpenAI-compatible proxy (for Crush/opencode integration)",
    )
    ap.add_argument("--port", type=int, default=8765, help="Port for --proxy mode (default: 8765)")
    ap.add_argument(
        "--opencode",
        action="store_true",
        help="Register the MCP tool with OpenCode (all projects); print provider config",
    )
    ap.add_argument(
        "--install-service",
        action="store_true",
        help="macOS: keep --proxy running via launchd (starts at login)",
    )
    ap.add_argument(
        "--uninstall-service", action="store_true", help="macOS: remove the proxy launchd service"
    )
    args = ap.parse_args()

    # ── Proxy service (macOS launchd) ────────────────────
    if args.install_service or args.uninstall_service:
        from .service import install, uninstall

        if args.uninstall_service:
            uninstall()
        else:
            install(port=args.port)
        return

    # ── OpenCode registration ────────────────────────────
    if args.opencode:
        from .opencode import install

        install(port=args.port)
        return

    # ── Clear session ────────────────────────────────────
    if args.clear_session:
        if args.chatgpt:
            from .chatgpt_creds import ChatGPTCredentials

            ChatGPTCredentials().clear()
            return
        if args.meta:
            from .meta_creds import MetaCredentials

            MetaCredentials().clear()
            return
        creds = CredentialManager()
        creds.clear()
        return

    # ── Login ────────────────────────────────────────────
    if args.login:
        do_login()
        return

    # ── Credential resolution ────────────────────────────
    # --chatgpt/--meta only acquire credentials; the proxy/MCP surfaces read
    # them from disk, so there is no provider REPL to fall through to.
    if args.chatgpt or args.meta:
        if args.meta:
            from . import meta_stream as provider_stream
            from .config import META_DEFAULT_MODEL, META_MODELS
            from .meta_creds import MetaCredentials as ProviderCredentials

            label, flag = "Meta AI", "--meta"
        else:
            from . import chatgpt_stream as provider_stream
            from .chatgpt_creds import ChatGPTCredentials as ProviderCredentials
            from .config import CHATGPT_DEFAULT_MODEL, CHATGPT_MODELS

            label, flag = "ChatGPT", "--chatgpt"
        models = META_MODELS if args.meta else CHATGPT_MODELS
        default_model = META_DEFAULT_MODEL if args.meta else CHATGPT_DEFAULT_MODEL

        pc = ProviderCredentials()
        if args.from_chrome is not None:
            ok = pc.from_chrome(args.from_chrome or None)
        elif args.manual:
            ok = pc.manual_input()
        elif args.prompt or args.proxy:
            ok = pc.load()
            if not ok:
                print(f"[!] No {label} credentials. Use {flag} --from-chrome or {flag} --manual")
                return
        else:
            print(f"[!] {flag} needs an action: --from-chrome, --manual, --proxy, or --prompt")
            return
        if not ok:
            print(f"[!] {label} credential acquisition failed")
            return
        if args.proxy:
            creds = CredentialManager()
            if not creds.load() or not creds.is_valid():
                print("[!] Proxy needs valid Claude credentials too (claude models are required).")
                return
            _run_proxy(creds, args.port)
            return
        if args.prompt:
            # --model defaults to the claude alias "sonnet"; only a real
            # provider slug the user typed is passed through.
            model = args.model if args.model in models else default_model
            session = {"conv_id": None, "created": False}
            try:
                provider_stream.stream_prompt(
                    pc,
                    args.prompt,
                    model=model,
                    stealth=not args.no_stealth,
                    session_state=session,
                )
            finally:
                # Cleanup on failure too: the conversation may exist even
                # when the turn was refused or timed out.
                if not args.no_stealth and session.get("created"):
                    provider_stream.delete_conversation(pc, session["conv_id"])
            return
        return

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
    elif (args.org_id or os.environ.get("SI_ORG_ID")) and (
        args.cookie or os.environ.get("SI_COOKIE")
    ):
        if args.cookie:
            print(
                "[!] Warning: --cookie exposes your sessionKey via `ps`/shell history;"
                " prefer the SI_COOKIE env var."
            )
        if _inline_creds_conflict(creds, args.org_id or os.environ.get("SI_ORG_ID")):
            print("[!] Stored credentials exist for a different org; keeping them.")
            print("    Run --clear-session first to replace them.")
            return
        creds.org_id = args.org_id or os.environ.get("SI_ORG_ID")
        creds._parse_cookies(args.cookie or os.environ.get("SI_COOKIE", ""))
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
        # ChatGPT and Meta models are served alongside claude ones when stored.
        _run_proxy(creds, args.port)
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

    lib = "curl_cffi"
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
        with open(args.jailbreak, encoding="utf-8") as f:
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
        with open(args.batch, encoding="utf-8") as f:
            prompts = [line.strip() for line in f if line.strip()]
        results = []
        try:
            for i, p in enumerate(prompts):
                print(f"  [{i + 1}/{len(prompts)}] {p[:60]}")
                r = stream_prompt(creds, p, model, stealth, session)
                results.append({"prompt": p, "response": r or ""})
                if i < len(prompts) - 1:
                    time.sleep(DEFAULT_BATCH_DELAY)
        finally:
            out = os.path.join(_CONFIG_DIR, "batch_results.json")
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
