"""opencode-style chat TUI for the Claude.ai RE client.

Runs the same streaming backend as the CLI, but inside a full-screen interface
shaped like opencode's chat page: a header bar, a scrolling message stream, a
composer, a keybind footer, and overlay dialogs for models / chats / help.

    .venv/bin/python -m claude_re --tui

Standard library only (curses + threading). The network call runs in a worker
thread and hands tokens back through a queue, so the UI never blocks.
"""
import contextlib
import curses
import io
import os
import queue
import threading

from .config import MODEL_ALIASES
from . import http as http_mod
from .streaming import stream_prompt

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# ── model objects ────────────────────────────────────────────────────

class Msg:
    __slots__ = ("role", "text", "streaming")

    def __init__(self, role, text="", streaming=False):
        self.role = role
        self.text = text
        self.streaming = streaming


class Chat:
    """One conversation: its own model, server conversation id and messages."""

    def __init__(self, model, title="new chat"):
        self.model = model
        self.title = title
        self.state = {}          # session_state passed to stream_prompt
        self.messages = []


# ── pure rendering helpers (no curses, so they are testable) ─────────

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
                lines.append(("  ┃ " + ln, "user"))
        elif m.role == "assistant":
            body = m.text + ("▌" if m.streaming else "")
            for ln in wrap(body, width):
                lines.append(("  " + ln, ""))
        else:  # note
            for ln in wrap(m.text, width):
                lines.append(("  " + ln, "note"))
        lines.append(("", ""))
    return lines


# ── the app ──────────────────────────────────────────────────────────

class App:
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
        if chat.title == "new chat":
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
            items = self.chats
            if sel >= len(items):
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
            return [f"{c.title}" for c in self.chats] + ["＋ new chat"]
        return [
            "enter / ctrl+s    send message",
            "ctrl+n            new chat",
            "ctrl+o            switch model",
            "ctrl+a            switch chat",
            "↑ ↓  pgup pgdn     scroll messages",
            "← →  home end      move cursor",
            "esc               clear input",
            "ctrl+c / ctrl+d   quit",
        ]

    # ── curses surface ───────────────────────────────────────────────
    def attr(self, kind):
        m = {"": (0, 0), "user": (2, curses.A_BOLD), "note": (4, 0),
             "accent": (1, curses.A_BOLD), "dim": (6, 0), "good": (3, 0),
             "warn": (4, 0), "bad": (5, 0), "title": (1, curses.A_BOLD)}
        c, a = m.get(kind, (0, 0))
        return (curses.color_pair(c) | a) if c else a

    def run(self, scr):
        curses.curs_set(1)
        curses.raw()                # cbreak leaves ^O (discard) and ^Q (XON) to the tty
        curses.use_default_colors()
        for i, c in enumerate([curses.COLOR_CYAN, curses.COLOR_BLUE,
                               curses.COLOR_GREEN, curses.COLOR_YELLOW,
                               curses.COLOR_RED, curses.COLOR_MAGENTA]):
            curses.init_pair(i + 1, c, -1)
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
        if h < 6 or w < 24:
            try:
                scr.addstr(0, 0, "terminal too small")
            except curses.error:
                pass
            scr.refresh()
            return
        try:
            self._header(scr, w)
            self._stream(scr, h, w)
            self._composer(scr, h, w)
            self._footer(scr, h, w)
            if self.overlay:
                self._overlay(scr, h, w)
        except curses.error:
            pass
        scr.refresh()

    def _header(self, scr, w):
        chat = self.chat()
        scr.addstr(0, 0, "⌬ ", self.attr("accent"))
        scr.addstr(0, 2, "reclaude", self.attr("accent"))
        scr.addstr(0, 11, "chat", self.attr("dim"))
        title = trunc(chat.title, max(8, w - 44))
        if w > len(title) + 24:
            scr.addstr(0, (w - len(title)) // 2, title, curses.A_BOLD)
        right = chat.model
        if w > len(right) + 2:
            scr.addstr(0, w - len(right) - 1, right, self.attr("dim"))

    def _stream(self, scr, h, w):
        vh = h - 3
        body = render_messages(self.chat(), w - 4)
        maxoff = max(0, len(body) - vh)
        if self.follow:
            self.off = maxoff
        self.off = max(0, min(self.off, maxoff))
        for i, (text, kind) in enumerate(body[self.off:self.off + vh]):
            scr.addstr(1 + i, 0, text[:w - 1], self.attr(kind))

    def _composer(self, scr, h, w):
        y = h - 2
        scr.addstr(y, 0, "┃ ", self.attr("accent"))
        if not self.buf and not self.busy:
            scr.addstr(y, 2, trunc("Message…", w - 3), self.attr("dim"))
            return
        start = max(0, self.cur - (w - 4))
        scr.addstr(y, 2, self.buf[start:start + w - 3], 0)
        scr.move(y, 2 + (self.cur - start))

    def _footer(self, scr, h, w):
        hint = ("enter send · ctrl+n new · ctrl+o model · ctrl+a chats · "
                "↑↓ scroll · ctrl+c quit")
        scr.addstr(h - 1, 0, trunc(hint, w - 12), self.attr("dim"))
        if self.busy:
            s = f" {SPINNER[self.spin // 2 % len(SPINNER)]} thinking "
            scr.addstr(h - 1, max(0, w - len(s) - 1), s, self.attr("accent"))

    def _overlay(self, scr, h, w):
        items = self.items()
        title = {"model": "Model", "chat": "Chats", "help": "Keys"}[self.overlay]
        bw = min(w - 4, max([42] + [len(i) + 6 for i in items]))
        bh = min(h - 2, len(items) + 2)
        x, y = (w - bw) // 2, (h - bh) // 2
        top, bot = "┌" + "─" * (bw - 2) + "┐", "└" + "─" * (bw - 2) + "┘"
        scr.addstr(y, x, top, self.attr("accent"))
        scr.addstr(y, x + 2, f" {title} ", self.attr("title"))
        scr.addstr(y + bh - 1, x, bot, self.attr("accent"))
        for r in range(1, bh - 1):
            scr.addstr(y + r, x, "│", self.attr("accent"))
            scr.addstr(y + r, x + bw - 1, "│", self.attr("accent"))
        scroll = max(0, self.ov_sel - (bh - 2) + 1)
        for i, item in enumerate(items[scroll:scroll + bh - 2]):
            row = y + 1 + i
            sel = (scroll + i) == self.ov_sel and self.overlay != "help"
            scr.addstr(row, x + 2, trunc(item, bw - 3).ljust(bw - 3),
                       curses.A_REVERSE if sel else 0)

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
