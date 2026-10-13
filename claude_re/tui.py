"""opencode-style chat TUI for the Claude.ai RE client.

Mirrors opencode's session view: a session tab strip on top, a message stream
with a right-hand Context sidebar, a boxed composer (┃ … ╹▀▀▀) with placeholder
and a `Build · model` status row, and a footer showing `~/dir:branch` and the
keybinds. Same streaming backend as the CLI; the request runs in a worker thread
and hands tokens back through a queue so the UI never blocks.

    .venv/bin/python -m claude_re --tui

Standard library only (curses + threading).
"""
import contextlib
import curses
import io
import os
import queue
import threading

from . import http as http_mod
from .config import MODEL_ALIASES
from .streaming import stream_prompt

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# opencode palette (RGB), approximated with the 256-colour cube below.
BASE = 233          # #121212  background
PANEL = 236         # #303030  composer / dialog background
TEXT = 255          # #eeeeee
DIM = 245           # #8a8a8a
GREEN = 114         # #87d787  additions / success
RED = 204           # #ff5f87  errors
PURPLE = 141        # #af87ff  reasoning
WARM = 180          # #d7af87  accents / logo

# 3-row block font for the empty-state word.
GLYPHS = {
    "r": ["█▀▄", "█▀▄", "▀ ▀"],
    "e": ["█▀▀", "██▄", "▀▀▀"],
    "c": ["█▀▀", "█  ", "▀▀▀"],
    "l": ["█  ", "█  ", "▀▀▀"],
    "a": ["█▀█", "█▀█", "▀ ▀"],
    "u": ["█ █", "█ █", "▀▀▀"],
    "d": ["█▄▄", "█ █", "▀▀▀"],
    "n": ["█▀█", "█ █", "▀ ▀"],
    "o": ["█▀█", "█ █", "▀▀▀"],
}


def block_word(word):
    """Render `word` (only letters present in GLYPHS) as 3 rows of block text."""
    rows = ["", "", ""]
    for i, ch in enumerate(word):
        g = GLYPHS.get(ch)
        if not g:
            continue
        for r in range(3):
            rows[r] += ("" if i == 0 else " ") + g[r]
    return rows


# ── model objects ────────────────────────────────────────────────────

class Msg:
    __slots__ = ("role", "text", "streaming")

    def __init__(self, role, text="", streaming=False):
        self.role = role
        self.text = text
        self.streaming = streaming


class Chat:
    """One conversation: its own model, server conversation id and messages."""

    def __init__(self, model, title="New session"):
        self.model = model
        self.title = title
        self.state = {}          # session_state passed to stream_prompt
        self.messages = []


# ── pure rendering helpers (testable without curses) ─────────────────

def trunc(s, n):
    s = str(s)
    return s if len(s) <= n else s[:max(0, n - 1)] + "…"


def wrap(text, width):
    """Word-wrap into lines no longer than `width`; long words are hard-split."""
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


def render_messages(chat, width):
    """Return [(text, kind)] lines for the message stream."""
    lines = []
    for m in chat.messages:
        if m.role == "user":
            for ln in wrap(m.text, width):
                lines.append(("┃ " + ln, "user"))
        elif m.role == "assistant":
            body = m.text + ("▌" if m.streaming else "")
            for ln in wrap(body, width):
                lines.append(("  " + ln, ""))
        else:  # note
            for ln in wrap(m.text, width):
                lines.append(("  " + ln, "note"))
        lines.append(("", ""))
    return lines


def cwd_label():
    cwd = os.getcwd()
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        cwd = "~" + cwd[len(home):]
    branch = ""
    try:
        with open(os.path.join(os.getcwd(), ".git", "HEAD")) as f:
            head = f.read().strip()
        if head.startswith("ref: refs/heads/"):
            branch = head[len("ref: refs/heads/"):]
    except OSError:
        pass
    return f"{cwd}:{branch}" if branch else cwd


# ── the app ──────────────────────────────────────────────────────────

class App:
    SIDEBAR = 34          # right Context panel width

    def __init__(self, creds, model, stealth):
        self.creds = creds
        self.stealth = stealth
        self.chats = [Chat(model)]
        self.active = 0
        self.buf = ""
        self.cur = 0
        self.follow = True
        self.off = 0
        self.overlay = None
        self.ov_sel = 0
        self.queue = queue.Queue()
        self.busy = False
        self.spin = 0
        self._resp = None

    def chat(self):
        return self.chats[self.active]

    # ── sending ──────────────────────────────────────────────────────
    def send(self):
        text = self.buf.strip()
        if not text or self.busy:
            return
        chat = self.chat()
        chat.messages.append(Msg("user", text))
        resp = Msg("assistant", "", streaming=True)
        chat.messages.append(resp)
        if chat.title == "New session":
            chat.title = text[:48]
        self.buf, self.cur, self.follow = "", 0, True
        self.busy, self._resp = True, resp
        threading.Thread(target=self._worker, args=(chat, resp, text),
                         daemon=True).start()

    def _worker(self, chat, resp, text):
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                stream_prompt(self.creds, text, chat.model, self.stealth,
                              chat.state,
                              on_text=lambda t: self.queue.put(("text", resp, t)))
        except Exception as e:                       # noqa: BLE001
            self.queue.put(("note", None, f"[!] {e}"))
        else:
            note = out.getvalue().strip()
            if any(k in note for k in ("[!]", "Rate limited", "Falling back",
                                       "Waiting", "expired", "not available")):
                self.queue.put(("note", None, note))
        self.queue.put(("done", None, None))

    def drain(self):
        changed = False
        while True:
            try:
                kind, resp, val = self.queue.get_nowait()
            except queue.Empty:
                return changed
            changed = True
            if kind == "text":
                resp.text += val
            elif kind == "note":
                self.chat().messages.append(Msg("note", val))
            elif kind == "done":
                self.busy = False
                if self._resp:
                    self._resp.streaming = False
                    self._resp = None

    # ── actions ──────────────────────────────────────────────────────
    def new_chat(self):
        if self.busy:
            return
        self.chats.append(Chat(self.chat().model))
        self.active = len(self.chats) - 1
        self.follow = True

    def set_model(self, alias):
        if self.busy:
            return
        chat = self.chat()
        chat.model = MODEL_ALIASES.get(alias, alias)
        chat.state = {}            # model change needs a fresh conversation
        chat.messages.append(Msg("note", f"model → {alias} ({chat.model})"))

    def pick(self, overlay, sel):
        if overlay == "model":
            self.set_model(self.model_items()[sel][1])
        elif overlay == "chat":
            if sel >= len(self.chats):
                self.new_chat()
            else:
                self.active = sel
                self.follow = True

    def model_items(self):
        cur = self.chat().model
        out = []
        for alias, full in MODEL_ALIASES.items():
            tier = "PRO" if any(k in alias for k in ("opus", "fable", "mythos")) else "FREE"
            out.append((f"{alias:<14} {full:<36} [{tier}]{'  ←' if full == cur else ''}",
                        alias))
        return out

    def items(self):
        if self.overlay == "model":
            return [label for label, _ in self.model_items()]
        if self.overlay == "chat":
            return [c.title for c in self.chats] + ["＋ new chat"]
        return [
            "enter / ctrl+s    send message",
            "ctrl+n            new session",
            "ctrl+o            switch model",
            "ctrl+a            switch session",
            "↑ ↓  pgup pgdn     scroll messages",
            "← →  home end      move cursor",
            "esc               clear input",
            "ctrl+c / ctrl+d   quit",
        ]

    # ── colours ──────────────────────────────────────────────────────
    def _init_colors(self):
        curses.start_color()
        if curses.COLORS >= 256:
            pairs = [(TEXT, BASE), (DIM, BASE), (GREEN, BASE), (RED, BASE),
                     (PURPLE, BASE), (WARM, BASE), (231, BASE), (TEXT, PANEL),
                     (DIM, PANEL), (240, BASE), (WARM, PANEL)]
        else:  # basic fallback
            pairs = [(curses.COLOR_WHITE, -1), (curses.COLOR_WHITE, -1),
                     (curses.COLOR_GREEN, -1), (curses.COLOR_RED, -1),
                     (curses.COLOR_MAGENTA, -1), (curses.COLOR_YELLOW, -1),
                     (curses.COLOR_WHITE, -1), (curses.COLOR_WHITE, -1),
                     (curses.COLOR_WHITE, -1), (curses.COLOR_WHITE, -1),
                     (curses.COLOR_YELLOW, -1)]
        for i, (fg, bg) in enumerate(pairs, 1):
            curses.init_pair(i, fg, bg)

    # pair indices
    P_TEXT, P_DIM, P_GREEN, P_RED, P_PURPLE, P_WARM = 1, 2, 3, 4, 5, 6
    P_WHITE, P_PTEXT, P_PDIM, P_BORDER, P_PWARM = 7, 8, 9, 10, 11

    def attr(self, kind):
        m = {"": self.P_TEXT, "user": self.P_WHITE, "note": self.P_PURPLE,
             "dim": self.P_DIM, "green": self.P_GREEN, "red": self.P_RED,
             "warm": self.P_WARM, "border": self.P_BORDER,
             "panel": self.P_PTEXT, "panel-dim": self.P_PDIM,
             "panel-warm": self.P_PWARM}
        return curses.color_pair(m.get(kind, self.P_TEXT))

    # ── curses surface ───────────────────────────────────────────────
    def run(self, scr):
        curses.curs_set(1)
        curses.raw()                # cbreak leaves ^O (discard) and ^Q (XON) to the tty
        curses.use_default_colors()
        self._init_colors()
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
        if h < 12 or w < 40:
            try:
                scr.addstr(0, 0, "terminal too small", self.attr(""))
            except curses.error:
                pass
            scr.refresh()
            return
        try:
            self._fill(scr, h, w)
            self._tabs(scr, w)
            self._body(scr, h, w)
            self._composer(scr, h, w)
            self._footer(scr, h, w)
            self._statusbar(scr, h, w)
            if self.overlay:
                self._overlay(scr, h, w)
        except curses.error:
            pass
        scr.refresh()

    def _fill(self, scr, h, w):
        for y in range(h):
            scr.addstr(y, 0, " " * (w - 1), self.attr(""))
            try:
                scr.addch(y, w - 1, " ", self.attr(""))
            except curses.error:
                pass

    def side_w(self, w):
        # opencode only shows the Context panel once a conversation exists.
        return self.SIDEBAR if (w >= 88 and self.chat().messages) else 0

    def _tabs(self, scr, w):
        # session tab strip: spinner + active title, other titles, then "+ New session"
        x = 1
        for i, c in enumerate(self.chats):
            spin = f"{SPINNER[self.spin // 2 % len(SPINNER)]} " if (self.busy and i == self.active) else ""
            label = f"{spin}{trunc(c.title, 30)}"
            if x + len(label) >= w - 15:
                break
            scr.addstr(0, x, label, self.attr("warm") if i == self.active else self.attr("dim"))
            x += len(label) + 3
        scr.addstr(0, x, "+ New session", self.attr("dim"))

    def main_w(self, w):
        sw = self.side_w(w)
        return w - sw - (1 if sw else 0)

    def _layout(self, h):
        """Return (empty, composer_top, footer_row). Empty chats centre the
        logo+composer group like opencode; a live chat pins it to the bottom."""
        if self.chat().messages:
            return False, h - 7, h - 2
        top = max(1, (h - 12) // 2)
        return True, top + 6, top + 11

    def _body(self, scr, h, w):
        mw = self.main_w(w)
        top, bottom = 1, h - 7           # message region rows
        vh = bottom - top
        chat = self.chat()

        if not chat.messages:
            self._empty_state(scr, self._layout(h)[1], mw, h)
        else:
            body = render_messages(chat, mw)
            maxoff = max(0, len(body) - vh)
            if self.follow:
                self.off = maxoff
            self.off = max(0, min(self.off, maxoff))
            for i, (text, kind) in enumerate(body[self.off:self.off + vh]):
                scr.addstr(top + i, 0, text[:mw], self.attr(kind))

        sw = self.side_w(w)
        if sw:
            self._sidebar(scr, top, bottom, w - sw, sw)

    def _empty_state(self, scr, comp_top, width, h):
        g = max(1, comp_top - 6)         # group top: logo, then hint
        logo = block_word("reclaude")
        for i, row in enumerate(logo):
            if row:
                scr.addstr(g + i, max(0, (width - len(row)) // 2), row, self.attr("warm"))
        hint = "Ask anything below"
        scr.addstr(g + 4, max(0, (width - len(hint)) // 2), hint, self.attr("dim"))

    def _sidebar(self, scr, top, bottom, x, sw):
        for y in range(top, bottom):
            scr.addstr(y, x - 1, "│", self.attr("border"))
        chat = self.chat()
        chars = sum(len(m.text) for m in chat.messages)
        rows = [
            ("Context", "panel-warm"),
            (f"~{chars // 4:,} tokens", "panel"),
            (f"{len(chat.messages)} messages", "panel-dim"),
            ("", "panel"),
            ("Model", "panel-warm"),
            (trunc(chat.model, sw - 2), "panel"),
            ("FREE", "panel-dim"),
        ]
        for i, (text, kind) in enumerate(rows):
            if top + i < bottom:
                scr.addstr(top + i, x, trunc(text, sw - 1), self.attr(kind))

    def _composer(self, scr, h, w):
        mw = self.main_w(w)
        left = 1
        cw = mw - 3                      # box width (gutter left, gap right)
        top = self._layout(h)[1]
        chat = self.chat()
        for y in range(top, top + 5):    # panel background behind the box
            scr.addstr(y, 0, " " * mw, self.attr("panel"))

        def box_row(y, text, kind="panel"):
            scr.addstr(y, left, "┃", self.attr("panel-warm"))
            if text:
                scr.addstr(y, left + 3, trunc(text, cw - 4), self.attr(kind))

        box_row(top, "")
        content = self.buf if self.buf else 'Ask anything… "Fix broken tests"'
        start = max(0, self.cur - (cw - 6)) if self.buf else 0
        box_row(top + 1, content[start:], "panel" if self.buf else "panel-dim")
        box_row(top + 2, "")
        box_row(top + 3, f"Build · {chat.model}", "panel-warm")
        scr.addstr(top + 4, left, "╹" + "▀" * (cw - 1), self.attr("panel-warm"))
        if self.buf:
            try:
                scr.move(top + 1, left + 3 + (self.cur - start))
            except curses.error:
                pass

    def _footer(self, scr, h, w):
        mw = self.main_w(w)
        y = self._layout(h)[2]
        scr.addstr(y, 4, trunc(cwd_label(), max(0, mw // 2)), self.attr("dim"))
        right = "esc interrupt   ctrl+c quit" if self.busy else \
            "ctrl+o models   ctrl+n new   ? help"
        scr.addstr(y, max(4, mw - len(right) - 2), right, self.attr("dim"))

    def _statusbar(self, scr, h, w):
        scr.addstr(h - 1, 2, f"⊙ {len(self.chats)} session  /help", self.attr("dim"))
        tag = "reclaude"
        scr.addstr(h - 1, max(2, w - len(tag) - 2), tag, self.attr("dim"))

    def _overlay(self, scr, h, w):
        items = self.items()
        title = {"model": "Model", "chat": "Sessions", "help": "Keys"}[self.overlay]
        bw = min(w - 4, max([44] + [len(i) + 6 for i in items]))
        bh = min(h - 2, len(items) + 2)
        x, y = (w - bw) // 2, (h - bh) // 2
        for r in range(bh):
            scr.addstr(y + r, x, " " * bw, self.attr("panel"))
        scr.addstr(y, x, "┌" + "─" * (bw - 2) + "┐", self.attr("warm"))
        scr.addstr(y, x + 2, f" {title} ", self.attr("panel-warm"))
        scr.addstr(y + bh - 1, x, "└" + "─" * (bw - 2) + "┘", self.attr("warm"))
        for r in range(1, bh - 1):
            scr.addstr(y + r, x, "│", self.attr("warm"))
            scr.addstr(y + r, x + bw - 1, "│", self.attr("warm"))
        scroll = max(0, self.ov_sel - (bh - 2) + 1)
        for i, item in enumerate(items[scroll:scroll + bh - 2]):
            row = y + 1 + i
            sel = (scroll + i) == self.ov_sel and self.overlay != "help"
            a = self.attr("panel") | (curses.A_REVERSE if sel else 0)
            scr.addstr(row, x + 2, trunc(item, bw - 3).ljust(bw - 3), a)

    # ── input ────────────────────────────────────────────────────────
    def key(self, ch):
        if self.overlay:
            return self._overlay_key(ch)
        if ch in (3, 4):                        # ctrl+c / ctrl+d
            return False
        if ch == curses.KEY_F1 or (ch == ord("?") and not self.buf):
            self._open("help")
        elif ch in (10, 13, 19):                # enter / ctrl+s
            self.send()
        elif ch == 14:                          # ctrl+n
            self.new_chat()
        elif ch == 15:                          # ctrl+o
            self._open("model")
        elif ch == 1:                           # ctrl+a
            self._open("chat")
        elif ch in (curses.KEY_UP,):
            self.follow = False
            self.off -= 1
        elif ch in (curses.KEY_DOWN,):
            self.off += 1
        elif ch == curses.KEY_PPAGE:
            self.follow = False
            self.off -= 10
        elif ch == curses.KEY_NPAGE:
            self.off += 10
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
        elif ch == 27:                          # esc clears the input
            self.buf, self.cur = "", 0
        elif 32 <= ch < 0x110000:
            self.buf = self.buf[:self.cur] + chr(ch) + self.buf[self.cur:]
            self.cur += 1
        return True

    def _overlay_key(self, ch):
        n = len(self.items())
        if ch in (3, 4):                        # ctrl+c / ctrl+d always quit
            return False
        if ch == 27:                            # esc closes the dialog
            self.overlay = None
        elif ch in (curses.KEY_UP, ord("k")):
            self.ov_sel = (self.ov_sel - 1) % n
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.ov_sel = (self.ov_sel + 1) % n
        elif ch in (10, 13, curses.KEY_ENTER):
            self.pick(self.overlay, self.ov_sel)
            self.overlay = None
        return True

    def _open(self, which):
        if which != "help" and self.busy:
            return
        self.overlay = which
        self.ov_sel = self.active if which == "chat" else 0


def run_tui(creds, model, stealth):
    """Entry point used by the CLI (`--tui`)."""
    app = App(creds, model, stealth)
    try:
        curses.wrapper(app.run)
    finally:
        cleanup(app)


def cleanup(app):
    """Same stealth guarantee as the CLI: delete conversations on exit."""
    if not app.stealth:
        return
    for chat in app.chats:
        cid = chat.state.get("conv_id")
        if cid and chat.state.get("created"):
            http_mod.delete_conversation(app.creds, cid)
