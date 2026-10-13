"""Usage dashboard: delegate to opencode's own `stats` CLI scoped to this project.

No reimplementation — `opencode stats` already renders the activity heatmap,
session/token/tool summaries from its usage DB. We just pin `--project .`
so it shows the project in the current directory, and pass dashboard flags
through untouched.
"""
import os
import shutil
import sys


def stats_argv(args):
    """Build the `opencode stats ...` argv from our parsed flags."""
    argv = ["stats", "--project", "."]
    if getattr(args, "days", None) is not None:
        argv += ["--days", str(args.days)]
    if getattr(args, "year", None) is not None:
        argv += ["--year", str(args.year)]
    for flag in ("all", "models", "tools", "cost", "full", "json"):
        if getattr(args, flag, False):
            argv.append(f"--{flag}")
    if getattr(args, "limit", None) is not None:
        argv += ["--limit", str(args.limit)]
    return argv


def run_stats(args):
    """Replace this process with `opencode stats --project . ...`."""
    exe = shutil.which("opencode")
    if not exe:
        print("[!] opencode CLI not found on PATH (brew install opencode-v2?)")
        return
    os.execvp(exe, [exe, *stats_argv(args)])


if __name__ == "__main__":  # `python -m claude_re.stats` behaves like --stats
    import argparse

    ap = argparse.ArgumentParser(description="opencode usage dashboard for this project")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--models", action="store_true")
    ap.add_argument("--tools", action="store_true")
    ap.add_argument("--cost", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    run_stats(ap.parse_args())
    sys.exit(0)
