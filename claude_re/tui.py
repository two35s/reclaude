"""Simple full-screen chat for the Claude.ai RE client.

One conversation, one input line, no chrome worship: a header, the message
stream, an input row, and a keybind footer. Keystrokes echo instantly;
network runs in a worker thread and streams tokens back through a queue.

    .venv/bin/python -m claude_re --tui

Standard library only (curses + threading).
"""

import contextlib
import curses
import io
import queue
import threading

from . import http as http_mod
from .config import MODEL_ALIASES
from .streaming import stream_prompt

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

COMMANDS = [
    ("/new", "new conversation"),
    ("/model", "switch model"),
    ("/models", "list models"),
    ("/help", "this list"),
    ("/quit", "exit"),
]


def trunc(s, n):
    s = str(s)
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def box_frame(width, left="", right=""):
    """Top/bottom rows of the input box; label left on top, right on bottom."""
    width = max(12, width)
    inner = width - 2

    def fill(label, align_left):
        tag = f"─ {trunc(label, max(0, inner - 4))} " if label else "─"
        tag = tag[:inner]
        if align_left:
            return (tag + "─" * inner)[:inner]
        return ("─" * max(0, inner - len(tag)) + tag)[:inner]

    return "╭" + fill(left, True) + "╮", "╰" + fill(right, False) + "╯"


def layout(h):
    """Message-region bottom row + compact-input flag for a height.

    Short terminals (<10 rows) get a single-line input instead of the box
    so the message stream keeps at least a few rows.
    """
    compact = h < 10
    return (h - 2 if compact else h - 4), compact


def dialog_window(h, w, n_items, title, hint, items_w):
    """Centered dialog geometry: (x, y, width, height, visible rows)."""
    bw = min(w - 2, max(len(title) + 8, items_w + 6, len(hint) + 6))
    bh = min(h - 2, n_items + 2)
    return (w - bw) // 2, (h - bh) // 2, bw, bh, max(1, bh - 2)


def wrap(text, width):
    """Word-wrap into lines no longer than `width`; long words hard-split."""
    width = max(1, width)
    out = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            if not line:
                line = word
            elif len(line) + 1 + len(word) <= width:
                line += " " + word
            else:
                out.append(line)
                line = word
            while len(line) > width:
                out.append(line[:width])
                line = line[width:]
        out.append(line)
    return out or [""]


def msg_lines(role, text, width, streaming=False):
    """Render one message to [(text, kind)] lines. Pure; tested."""
    body = text + ("▌" if streaming else "")
    if role == "note":
        return [(f"({ln})", "dim") for ln in wrap(body, width - 2)]
    if role == "user":
        lines = wrap(body, width - 2)
        return [(("> " if i == 0 else "  ") + ln, "user") for i, ln in enumerate(lines)]
    return [(ln, "") for ln in wrap(body, width)]


class App:
    def __init__(self, creds, model, stealth):
        self.creds = creds
        self.stealth = stealth
        self.model = model
        self.state = {}
        self.messages = []  # (role, text); live text mutates in place
        self.buf = ""
        self.cur = 0
        self.off = 0  # scroll offset from bottom; 0 = following
        self.dialog = None  # None | "models" | "help"
        self.sel = 0
        self.queue = queue.Queue()
        self.busy = False
        self.spin = 0
        self.cmd_sel = 0
        self._cmd_buf = None

    # ── sending ──────────────────────────────────────────────
    def send(self):
        text = self.buf.strip()
        if not text or self.busy:
            return
        self.buf, self.cur, self.off = "", 0, 0
        self.messages.append(["user", text])
        live = ["assistant", ""]
        self.messages.append(live)
        self.off = 0
        self.busy = True
        threading.Thread(target=self._worker, args=(live, text), daemon=True).start()

    def cmd_items(self):
        """Slash completions for the current input; resets selection on change."""
        if not self.buf.startswith("/") or " " in self.buf or self.dialog or self.busy:
            return []
        if self.buf != self._cmd_buf:
            self._cmd_buf, self.cmd_sel = self.buf, 0
        frag = self.buf[1:].lower()
        return [(c, d) for c, d in COMMANDS if c[1:].startswith(frag)]

    def _command(self, text):
        """Slash commands (no network). Returns True to quit."""
        parts = text[1:].split(None, 1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("quit", "exit", "q"):
            return True
        if cmd == "new":
            self.fresh()
        elif cmd == "model":
            if arg:
                self.set_model(arg)
            else:
                self.dialog, self.sel = "models", 0
        elif cmd == "models":
            names = ", ".join(MODEL_ALIASES)
            self.messages.append(["note", f"models: {names}"])
        elif cmd == "help":
            self.dialog, self.sel = "help", 0
        else:
            self.messages.append(["note", f"unknown command: {text} — try /help"])
        self.off = 0
        return False

    def _worker(self, live, text):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                stream_prompt(
                    self.creds,
                    text,
                    self.model,
                    self.stealth,
                    self.state,
                    on_text=lambda t: self.queue.put(("text", t)),
                )
        except Exception as e:
            self.queue.put(("note", f"error: {e}"))
        self.queue.put(("done", None))

    def drain(self):
        while True:
            try:
                kind, val = self.queue.get_nowait()
            except queue.Empty:
                return
            if kind == "text":
                self.messages[-1][1] += val
            elif kind == "note":
                self.messages.append(["note", val])
            elif kind == "done":
                self.busy = False

    # ── actions ──────────────────────────────────────────────
    def fresh(self):
        """New conversation; stealth-deletes the old one right away."""
        if self.busy:
            return
        self._cleanup()
        self.state = {}
        self.messages = []
        self.off = 0

    def set_model(self, alias):
        if self.busy:
            return
        self.model = MODEL_ALIASES.get(alias, alias)
        self.fresh()
        self.messages.append(["note", f"model: {alias} ({self.model})"])

    def _cleanup(self):
        if self.stealth and self.state.get("conv_id") and self.state.get("created"):
            http_mod.delete_conversation(self.creds, self.state["conv_id"])
            self.state = {}

    # ── colours ──────────────────────────────────────────────
    def _colors(self):
        curses.start_color()
        curses.use_default_colors()
        if curses.COLORS >= 256:
            pairs = [(255, -1), (244, -1), (114, -1), (203, -1)]
        else:
            pairs = [
                (curses.COLOR_WHITE, -1),
                (curses.COLOR_WHITE, -1),
                (curses.COLOR_GREEN, -1),
                (curses.COLOR_RED, -1),
            ]
        for i, (fg, bg) in enumerate(pairs, 1):
            try:
                curses.init_pair(i, fg, bg)
            except curses.error:
                pass

    def attr(self, kind):
        return curses.color_pair({"user": 3, "dim": 2, "err": 4}.get(kind, 1))

    # ── run loop ─────────────────────────────────────────────
    def run(self, scr):
        curses.curs_set(1)
        curses.raw()  # cbreak keeps ^O/^Q for the tty; raw gives them to us
        self._colors()
        scr.keypad(True)
        scr.timeout(80)
        while True:
            self.drain()
            self.draw(scr)
            try:
                ch = scr.getch()
            except KeyboardInterrupt:
                break
            if ch == -1:
                if self.busy:
                    self.spin += 1
                continue
            if not self.key(ch):
                break

    def draw(self, scr):
        h, w = scr.getmaxyx()
        scr.erase()
        if h < 6 or w < 20:
            scr.addstr(0, 0, "terminal too small")
            scr.refresh()
            return
        try:
            self._header(scr, w)
            self._messages(scr, h, w)
            popup = self.cmd_items()
            if popup:
                self._cmd_popup(scr, h, w, popup)
            self._input(scr, h, w)
            self._footer(scr, h, w)
            if self.dialog:
                self._dialog(scr, h, w)
        except curses.error:
            pass
        scr.refresh()

    def _header(self, scr, w):
        scr.addstr(0, 0, " reclaude ", curses.A_BOLD)
        scr.addstr(0, 10, self.model[: max(0, w - 12)], self.attr("user"))

    def _messages(self, scr, h, w):
        top, bottom = 1, layout(h)[0]
        lines = []
        for i, (role, text) in enumerate(self.messages):
            streaming = self.busy and i == len(self.messages) - 1 and role == "assistant"
            lines += msg_lines(role, text, w, streaming)
            lines.append(("", ""))
        if self.off <= 0:
            self.off = 0
            view = lines[-(bottom - top) :]
        else:
            self.off = min(self.off, max(0, len(lines) - (bottom - top)))
            end = len(lines) - self.off
            view = lines[max(0, end - (bottom - top)) : end]
        for i, (text, kind) in enumerate(view):
            a = self.attr(kind)
            if kind == "user":
                a |= curses.A_BOLD
            scr.addstr(top + i, 0, text[:w], a)

    def _input(self, scr, h, w):
        _, compact = layout(h)
        maxw = max(1, w - 6)
        start = max(0, self.cur - maxw + 1)
        if compact:  # short screen: prompt line only
            scr.addstr(h - 2, 0, " " * (w - 1))
            scr.addstr(h - 2, 0, "> ", self.attr("user") | curses.A_BOLD)
            scr.addstr(h - 2, 2, self.buf[start : start + maxw])
            if not self.buf and not self.busy:
                scr.addstr(h - 2, 2, "type + enter", self.attr("dim"))
            try:
                scr.move(h - 2, 2 + self.cur - start)
            except curses.error:
                pass
            return
        top, mid, bot = h - 4, h - 3, h - 2
        has_chat = any(r in ("user", "assistant") for r, _ in self.messages)
        state = "working…" if self.busy else ("live" if has_chat else "fresh")
        frame_top, frame_bot = box_frame(w, "message", f"{self.model} · {state}")
        scr.addstr(top, 0, frame_top, self.attr("dim"))
        scr.addstr(bot, 0, frame_bot, self.attr("dim"))
        scr.addstr(mid, 0, " " * (w - 1))  # clear stale chars first
        scr.addstr(mid, 0, "│", self.attr("dim"))
        scr.addstr(mid, w - 1, "│", self.attr("dim"))
        scr.addstr(mid, 2, "> ", self.attr("user") | curses.A_BOLD)
        maxw = max(1, w - 6)
        start = max(0, self.cur - maxw + 1)
        scr.addstr(mid, 4, self.buf[start : start + maxw])
        if not self.buf:
            hint = "waiting for reply…" if self.busy else "type a message, enter to send"
            scr.addstr(mid, 4, hint[:maxw], self.attr("dim"))
        try:
            scr.move(mid, 4 + self.cur - start)
        except curses.error:
            pass

    def _cmd_popup(self, scr, h, w, items):
        bw = min(w - 2, max(len(c) + len(d) + 5 for c, d in items) + 2)
        bh = len(items) + 2
        _, compact = layout(h)
        y = max(1, ((h - 2) if compact else (h - 4)) - bh)
        for r in range(bh):
            scr.addstr(y + r, 0, " " * (bw - 1), self.attr("panel"))
        top, bot = box_frame(bw, "commands", "tab pick · enter run")
        scr.addstr(y, 0, top, self.attr("dim"))
        scr.addstr(y + bh - 1, 0, bot, self.attr("dim"))
        for r in range(1, bh - 1):
            scr.addstr(y + r, 0, "│", self.attr("dim"))
            scr.addstr(y + r, bw - 1, "│", self.attr("dim"))
        for i, (c, d) in enumerate(items):
            a = self.attr("panel")
            if i == self.cmd_sel % len(items):
                a |= curses.A_REVERSE
            scr.addstr(y + 1 + i, 2, f"{c:<9} {d}"[: bw - 4], a)

    def _footer(self, scr, h, w):
        if self.busy:
            left = f" {SPINNER[self.spin % len(SPINNER)]} working…  esc clears input"
        elif w < 56:
            left = " /new /model /help /quit"
        else:
            left = " /new /model /models /help /quit · tab pick · enter send"
        scr.addstr(h - 1, 0, left[:w], self.attr("dim"))

    def _dialog(self, scr, h, w):
        if self.dialog == "models":
            items = [f"{a:<12} {f}" for a, f in MODEL_ALIASES.items()]
            title, hint, selectable = "model", "enter pick · esc close", True
        else:
            items = [
                "/new new conversation",
                "/model [name] switch model",
                "/models list models",
                "/help this list",
                "/quit exit",
                "enter send · ↑↓ scroll · esc clear · ^c quit",
            ]
            title, hint, selectable = "commands", "any key closes", False
        x, y, bw, bh, vis = dialog_window(h, w, len(items), title, hint, max(len(i) for i in items))
        for r in range(bh):
            scr.addstr(y + r, x, " " * (bw - 1), self.attr("panel"))
        top, bot = box_frame(bw, title, hint)
        scr.addstr(y, x, top, self.attr("dim"))
        scr.addstr(y + bh - 1, x, bot, self.attr("dim"))
        for r in range(1, bh - 1):
            scr.addstr(y + r, x, "│", self.attr("dim"))
            scr.addstr(y + r, x + bw - 1, "│", self.attr("dim"))
        start = max(0, min(self.sel - vis + 1, len(items) - vis)) if selectable else 0
        for i, item in enumerate(items[start : start + vis]):
            a = self.attr("panel")
            if selectable and start + i == self.sel:
                a |= curses.A_REVERSE
            scr.addstr(y + 1 + i, x + 2, item[: bw - 4], a)

    # ── input ────────────────────────────────────────────────
    def key(self, ch):
        if self.dialog:
            if self.dialog == "models":
                return self._model_key(ch)
            self.dialog = None
            return True
        if ch in (3, 4):
            return False
        if ch == curses.KEY_F1 or (ch == ord("?") and not self.buf):
            self.dialog, self.sel = "help", 0
        elif ch in (10, 13):
            if self._enter():
                return False
        elif ch == 14:
            self.fresh()
        elif ch == 15:
            if not self.busy:
                self.dialog, self.sel = "models", 0
        elif ch == curses.KEY_UP:
            items = self.cmd_items()
            if items:
                self.cmd_sel = (self.cmd_sel - 1) % len(items)
            else:
                self.off += 1
        elif ch == curses.KEY_DOWN:
            items = self.cmd_items()
            if items:
                self.cmd_sel = (self.cmd_sel + 1) % len(items)
            else:
                self.off = max(0, self.off - 1)
        elif ch == curses.KEY_PPAGE:
            self.off += 10
        elif ch == curses.KEY_NPAGE:
            self.off = max(0, self.off - 10)
        elif ch == curses.KEY_LEFT:
            self.cur = max(0, self.cur - 1)
        elif ch == curses.KEY_RIGHT:
            self.cur = min(len(self.buf), self.cur + 1)
        elif ch == curses.KEY_HOME:
            self.cur = 0
        elif ch == curses.KEY_END:
            self.cur = len(self.buf)
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            if self.cur > 0:
                self.buf = self.buf[: self.cur - 1] + self.buf[self.cur :]
                self.cur -= 1
        elif ch == curses.KEY_DC:
            self.buf = self.buf[: self.cur] + self.buf[self.cur + 1 :]
        elif ch == 27:
            self.buf, self.cur = "", 0
        elif ch == 9:
            items = self.cmd_items()
            if items:
                self.buf = items[self.cmd_sel % len(items)][0] + " "
                self.cur = len(self.buf)
        elif 32 <= ch < 0x110000:
            self.buf = self.buf[: self.cur] + chr(ch) + self.buf[self.cur :]
            self.cur += 1
        return True

    def _enter(self):
        """Enter key: slash lines run as commands (/quit exits); else send."""
        if self.buf.strip().startswith("/"):
            text = self.buf.strip()
            self.buf, self.cur = "", 0
            if self.busy:
                return False
            return self._command(text)
        self.send()
        return False

    def _model_key(self, ch):
        names = list(MODEL_ALIASES)
        if ch in (3, 4):
            return False
        if ch == 27:
            self.dialog = None
        elif ch == curses.KEY_UP:
            self.sel = (self.sel - 1) % len(names)
        elif ch == curses.KEY_DOWN:
            self.sel = (self.sel + 1) % len(names)
        elif ch in (10, 13):
            self.set_model(names[self.sel])
            self.dialog = None
        return True


def run_tui(creds, model, stealth):
    """Entry point used by the CLI (`--tui`)."""
    app = App(creds, model, stealth)
    try:
        curses.wrapper(app.run)
    finally:
        app._cleanup()
