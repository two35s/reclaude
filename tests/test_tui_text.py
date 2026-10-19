"""Message rendering stays within width and marks roles/streaming."""

import pytest

import si.tui as tui_mod
from si.tui import App, box_frame, dialog_window, layout, msg_lines, wrap


@pytest.mark.parametrize("width", [1, 5, 20])
def test_wrap_never_exceeds_width(width):
    for line in wrap("the quick brown fox\nsupercalifragilisticexpialidocious", width):
        assert len(line) <= width


def test_msg_lines_mark_roles_and_streaming():
    assert msg_lines("user", "hi", 40) == [("> hi", "user")]
    assert msg_lines("assistant", "hello", 40, streaming=True) == [("hello▌", "")]
    assert msg_lines("note", "model: sonnet (x)", 40) == [("(model: sonnet (x))", "dim")]


def test_msg_lines_continuation_indents():
    lines = msg_lines("user", "aa bb cc dd ee", 12)
    assert lines[0][0].startswith("> ")
    assert all(t.startswith("  ") for t, _ in lines[1:])
    assert all(len(t) <= 12 for t, _ in lines)


def test_box_frame_width_and_label_sides():
    top, bot = box_frame(30, "message", "sonnet · live")
    assert (len(top), len(bot)) == (30, 30)
    assert top.startswith("╭─ message ") and top.endswith("╮")
    assert bot.startswith("╰") and bot.endswith("sonnet · live ╯")
    plain_top, plain_bot = box_frame(30)
    assert plain_top == "╭" + "─" * 28 + "╮"
    assert plain_bot == "╰" + "─" * 28 + "╯"


def test_layout_compacts_on_short_terminals():
    assert layout(30) == (26, False)
    assert layout(10) == (6, False)
    assert layout(8) == (6, True)


def test_dialog_window_geometry():
    x, y, bw, bh, vis = dialog_window(30, 100, 9, "model", "enter pick · esc close", 30)
    assert (bw, bh, vis) == (36, 11, 9)
    assert (x, y) == ((100 - 36) // 2, (30 - 11) // 2)
    _, _, bw2, bh2, vis2 = dialog_window(8, 30, 9, "model", "hint", 30)
    assert (bw2, bh2, vis2) == (28, 6, 4)


def test_completed_messages_are_wrapped_once(monkeypatch):
    calls = []
    real = tui_mod.msg_lines

    def counted(*a, **k):
        calls.append(a[:2])
        return real(*a, **k)

    monkeypatch.setattr(tui_mod, "msg_lines", counted)
    monkeypatch.setattr(App, "attr", lambda self, kind: 0)

    class Scr:
        def addstr(self, *a, **k):
            pass

    app = App(None, "claude-sonnet-5-5", stealth=False)
    app.messages = [["user", "hello"], ["assistant", "world"]]
    app._messages(Scr(), 24, 80)
    first = len(calls)
    app._messages(Scr(), 24, 80)
    assert len(calls) == first  # unchanged messages come from the cache
