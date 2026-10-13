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
