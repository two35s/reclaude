"""REPL help + model listing."""

from .config import MODEL_ALIASES


def print_help():
    print("""
  ╔══════════════════════════════════════════════════════╗
  ║                    COMMANDS                         ║
  ╠══════════════════════════════════════════════════════╣
  ║  /model <name>     Switch model                     ║
  ║  /models           List all models                  ║
  ║  /stealth on|off   Toggle cleanup on exit           ║
  ║  /new              Start fresh conversation         ║
  ║  /cleanup          Delete session conv NOW          ║
  ║  /clear-session    Wipe stored credentials          ║
  ║  /help             Show this help                   ║
  ║  exit              Quit (auto-cleans if stealth)    ║
  ╚══════════════════════════════════════════════════════╝
""")


def print_models(current):
    print("\n  Available models:")
    for alias, full in MODEL_ALIASES.items():
        marker = " <--" if full == current else ""
        tier = "PRO" if any(k in alias for k in ("opus", "fable", "mythos")) else "FREE"
        print(f"    {alias:14s}  {full:36s}  [{tier}]{marker}")
    print()
