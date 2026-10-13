"""Slash commands run locally without network."""

import pytest

from claude_re.tui import App


@pytest.fixture
def app():
    return App(None, "claude-sonnet-5-5", stealth=False)


def test_unknown_command_becomes_note(app):
    assert app._command("/bogus") is False
    assert app.messages[-1][0] == "note"


def test_new_clears_and_model_switches(app):
    app.messages.append(["user", "hi"])
    app._command("/new")
    assert app.messages == []
    app._command("/model haiku")
    assert app.model == "claude-haiku-5-5"
    assert app.messages[-1][1].startswith("model:")


def test_models_opens_picker_on_active_row(app):
    assert app._command("/models") is False
    assert app.dialog == "models"
    assert app.sel == 1  # claude-sonnet-5-5 is second in MODEL_ALIASES
    rows = app.model_items()
    assert sum(active for _, active in rows) == 1
    assert rows[1][1] is True and rows[1][0].startswith("●")


def test_set_model_unchanged_keeps_messages(app):
    app.messages.append(["user", "hi"])
    app.set_model("sonnet")
    assert app.model == "claude-sonnet-5-5"
    assert [r for r, _ in app.messages] == ["user", "note"]


def test_run_uses_block_cursor(monkeypatch, app):
    import curses

    seen = {}
    monkeypatch.setattr(curses, "curs_set", lambda v: seen.setdefault("v", v))
    monkeypatch.setattr(curses, "raw", lambda: None)
    monkeypatch.setattr(App, "_colors", lambda self: None)
    monkeypatch.setattr(App, "attr", lambda self, kind: 0)

    class QuitScr:
        moves = []

        def keypad(self, flag):
            pass

        def timeout(self, ms):
            pass

        def getmaxyx(self):
            return (24, 80)

        def erase(self):
            pass

        def refresh(self):
            pass

        def addstr(self, y, x, s, *a):
            pass

        def move(self, y, x):
            QuitScr.moves.append((y, x))

        def getch(self):
            return 4  # Ctrl+D quits immediately

    app.run(QuitScr())
    assert seen["v"] == 2
    assert QuitScr.moves and QuitScr.moves[-1] == (21, 4)  # last move wins: input row


def test_partial_command_unique_prefix_runs(app):
    assert app._command("/n") is False
    assert app.messages == []
    assert app._command("/he") is False
    assert app.dialog == "help"


def test_partial_command_ambiguous_suggests(app):
    assert app._command("/mo") is False
    role, text = app.messages[-1]
    assert role == "note"
    assert "/model" in text and "/models" in text


def test_quit_and_help(app):
    assert app._command("/quit") is True
    assert app._command("/help") is False
    assert app.dialog == "help"


def test_slash_completion_filters_and_tab_picks(app):
    app.buf = "/m"
    assert [c for c, _ in app.cmd_items()] == ["/model", "/models"]
    app.buf = "/x"
    assert app.cmd_items() == []
    app.buf = "/model haiku"
    assert app.cmd_items() == []
    app.buf = "hi"
    assert app.cmd_items() == []
    app.buf = "/"
    app.cmd_items()  # settle: selection resets on input change
    app.cmd_sel = 3
    app.key(9)
    assert app.buf == "/help "
