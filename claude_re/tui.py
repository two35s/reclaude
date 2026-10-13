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
    if role == "user":
        return [(f"> {ln}", "user") for ln in wrap(body, width - 2)]
    if role == "note":
        return [(f"({ln})", "dim") for ln in wrap(body, width - 2)]
    return [(ln, "") for ln in wrap(body, width)]


class App:
    def __init__(self, creds, model, stealth):
        self.creds = creds
        self.stealth = stealth
        self.model = model
        self.state = {}
        self.messages = []            # (role, text); live text mutates in place
        self.buf = ""
        self.cur = 0
        self.off = 0                  # scroll offset from bottom; 0 = following
        self.dialog = None            # None | "models" | "help"
        self.sel = 0
        self.queue = queue.Queue()
        self.busy = False
        self.spin = 0

    # ── sending ──────────────────────────────────────────────
    def send(self):
        text = self.buf.strip()
        if not text or self.busy:
            return
        self.messages.append(["user", text])
        live = ["assistant", ""]
        self.messages.append(live)
        self.buf, self.cur, self.off = "", 0, 0
        self.busy = True
        threading.Thread(target=self._worker, args=(live, text),
                         daemon=True).start()

    def _worker(self, live, text):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                stream_prompt(self.creds, text, self.model, self.stealth,
                              self.state,
                              on_text=lambda t: self.queue.put(("text", t)))
        except Exception as e:                       # noqa: BLE001
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
            pairs = [(curses.COLOR_WHITE, -1), (curses.COLOR_WHITE, -1),
                     (curses.COLOR_GREEN, -1), (curses.COLOR_RED, -1)]
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
        curses.raw()                # cbreak keeps ^O/^Q for the tty; raw gives them to us
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
            self._input(scr, h, w)
            self._footer(scr, h, w)
            if self.dialog:
                self._dialog(scr, h, w)
        except curses.error:
            pass
        scr.refresh()

    def _header(self, scr, w):
        scr.addstr(0, 0, " reclaude ", curses.A_BOLD)
        scr.addstr(0, 10, self.model[: w - 12], self.attr("user"))

    def _messages(self, scr, h, w):
        top, bottom = 1, h - 3
        lines = []
        for i, (role, text) in enumerate(self.messages):
            streaming = self.busy and i == len(self.messages) - 1 and role == "assistant"
            lines += msg_lines(role, text, w, streaming)
            lines.append(("", ""))
        if self.off <= 0:
            self.off = 0
            view = lines[-(bottom - top):]
        else:
            self.off = min(self.off, max(0, len(lines) - (bottom - top)))
            end = len(lines) - self.off
            view = lines[max(0, end - (bottom - top)):end]
        for i, (text, kind) in enumerate(view):
            a = self.attr(kind)
            if kind == "user":
                a |= curses.A_BOLD
            scr.addstr(top + i, 0, text[:w], a)

    def _input(self, scr, h, w):
        scr.addstr(h - 3, 0, "─" * (w - 1), self.attr("dim"))
        prompt, maxw = "> ", w - 3
        scr.addstr(h - 2, 0, prompt, self.attr("user") | curses.A_BOLD)
        start = max(0, self.cur - maxw + 1)
        scr.addstr(h - 2, 2, self.buf[start:start + maxw])
        if not self.buf and not self.busy:
            scr.addstr(h - 2, 2, "type a message, enter to send", self.attr("dim"))
        try:
            scr.move(h - 2, 2 + self.cur - start)
        except curses.error:
            pass

    def _footer(self, scr, h, w):
        if self.busy:
            left = f" {SPINNER[self.spin % len(SPINNER)]} working…  esc clears input"
        else:
            left = " enter send · ^n new · ^o model · ? help · ^c quit"
        scr.addstr(h - 1, 0, left[:w], self.attr("dim"))

    def _dialog(self, scr, h, w):
        if self.dialog == "models":
            items = [f"{a:<12} {f}" for a, f in MODEL_ALIASES.items()]
            title = "model (enter picks, esc closes)"
        else:
            items = ["enter send", "^n new conversation", "^o switch model",
                     "up/down pgup/pgdn scroll", "left/right home/end cursor",
                     "esc clear input / close", "^c ^d quit"]
            title = "keys (any key closes)"
        bw = min(w - 4, max(len(title) + 4, max(len(i) for i in items) + 4))
        bh = min(h - 4, len(items) + 4)
        x, y = (w - bw) // 2, (h - bh) // 2
        for r in range(bh):
            scr.addstr(y + r, x, " " * (bw - 1), self.attr(""))
        scr.addstr(y, x, f" {title} "[:bw], curses.A_BOLD)
        vis = bh - 2
        start = max(0, min(self.sel - vis + 1, len(items) - vis)) if self.dialog == "models" else 0
        rows = items[start:start + vis]
        for i, item in enumerate(rows):
            a = curses.A_REVERSE if (self.dialog == "models" and start + i == self.sel) else 0
            scr.addstr(y + 2 + i, x + 2, item[:bw - 4], a)

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
            self.send()
        elif ch == 14:
            self.fresh()
        elif ch == 15:
            if not self.busy:
                self.dialog, self.sel = "models", 0
        elif ch == curses.KEY_UP:
            self.off += 1
        elif ch == curses.KEY_DOWN:
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
                self.buf = self.buf[:self.cur - 1] + self.buf[self.cur:]
                self.cur -= 1
        elif ch == curses.KEY_DC:
            self.buf = self.buf[:self.cur] + self.buf[self.cur + 1:]
        elif ch == 27:
            self.buf, self.cur = "", 0
        elif 32 <= ch < 0x110000:
            self.buf = self.buf[:self.cur] + chr(ch) + self.buf[self.cur:]
            self.cur += 1
        return True

    def _model_key(self, ch):
        names = list(MODEL_ALIASES)
        if ch in (3, 4):
            return False
        if ch == 27:
            self.dialog = None
        elif ch in (curses.KEY_UP,):
            self.sel = (self.sel - 1) % len(names)
        elif ch in (curses.KEY_DOWN,):
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
