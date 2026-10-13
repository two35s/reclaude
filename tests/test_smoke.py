"""Small regression tests for the risky, parser-heavy logic.

Run from the repo root:  .venv/bin/python -m unittest discover -s tests -v
"""
import unittest
from unittest import mock

from claude_re import chrome_cookies
from claude_re.config import MODEL_ALIASES, PAYLOAD_TEMPLATE, resolve_model
from claude_re.payload import build_payload
from claude_re.streaming import _sse_data, _split_lines
from claude_re.tui import box_frame, dialog_window, layout, msg_lines, wrap


class SseParserTest(unittest.TestCase):
    def test_multibyte_char_split_across_chunks(self):
        # A UTF-8 'é' (two bytes) split across two network chunks must survive.
        payload = 'data: {"text":"\u00e9"}\n'.encode("utf-8")
        cut = payload.index(b"\xc3") + 1
        buf, lines = _split_lines(b"", payload[:cut])
        self.assertEqual(lines, [])
        buf, lines = _split_lines(buf, payload[cut:])
        self.assertEqual(len(lines), 1)
        self.assertEqual(_sse_data(lines[0]), '{"text":"\u00e9"}')

    def test_split_lines_keeps_only_complete_lines(self):
        buf, lines = _split_lines(b"", b"a\nb\nc")
        self.assertEqual(lines, [b"a", b"b"])
        self.assertEqual(buf, b"c")

    def test_sse_data_ignores_non_data_lines(self):
        self.assertIsNone(_sse_data(b""))
        self.assertIsNone(_sse_data(b": keep-alive"))
        self.assertIsNone(_sse_data(b"event: message_stop"))
        self.assertEqual(_sse_data(b"data: [DONE]"), "[DONE]")


class ModelTest(unittest.TestCase):
    def test_resolve_alias_and_passthrough(self):
        self.assertEqual(resolve_model("sonnet"), MODEL_ALIASES["sonnet"])
        self.assertEqual(resolve_model("claude-sonnet-4-6"), "claude-sonnet-4-6")


class PayloadTest(unittest.TestCase):
    def test_unique_uuids_and_template_untouched(self):
        a = build_payload("hi")
        b = build_payload("hi")
        au, bu = a["turn_message_uuids"], b["turn_message_uuids"]
        self.assertNotEqual(au["human_message_uuid"], au["assistant_message_uuid"])
        self.assertNotEqual(au["human_message_uuid"], bu["human_message_uuid"])
        self.assertEqual(a["prompt"], "hi")
        self.assertEqual(PAYLOAD_TEMPLATE["prompt"], "")


class CookieDecryptTest(unittest.TestCase):
    """The 32-byte prefix decision must follow Chrome's schema version."""

    def test_prefix_stripped_when_version_new(self):
        plaintext = b"\x00" * 32 + b"session"
        with mock.patch.object(chrome_cookies, "_aes_decrypt",
                               return_value=plaintext + b"\x01"):
            value = chrome_cookies._decrypt(b"v10\x00", b"k" * 16,
                                            strip_prefix=True)
        self.assertEqual(value, "session")

    def test_prefix_kept_when_version_old(self):
        with mock.patch.object(chrome_cookies, "_aes_decrypt",
                               return_value=b"session\x01"):
            value = chrome_cookies._decrypt(b"v10\x00", b"k" * 16,
                                            strip_prefix=False)
        self.assertEqual(value, "session")


class TuiTextTest(unittest.TestCase):
    """Message rendering stays within width and marks roles/streaming."""

    def test_wrap_never_exceeds_width(self):
        for width in (1, 5, 20):
            for line in wrap("the quick brown fox\nsupercalifragilisticexpialidocious", width):
                self.assertLessEqual(len(line), width)

    def test_msg_lines_mark_roles_and_streaming(self):
        self.assertEqual(msg_lines("user", "hi", 40), [("> hi", "user")])
        self.assertEqual(msg_lines("assistant", "hello", 40, streaming=True),
                         [("hello▌", "")])
        note = msg_lines("note", "model: sonnet (x)", 40)
        self.assertEqual(note, [("(model: sonnet (x))", "dim")])

    def test_msg_lines_continuation_indents(self):
        lines = msg_lines("user", "aa bb cc dd ee", 12)
        self.assertTrue(lines[0][0].startswith("> "))
        self.assertTrue(all(t.startswith("  ") for t, _ in lines[1:]))
        self.assertTrue(all(len(t) <= 12 for t, _ in lines))

    def test_box_frame_width_and_label_sides(self):
        top, bot = box_frame(30, "message", "sonnet · live")
        self.assertEqual((len(top), len(bot)), (30, 30))
        self.assertTrue(top.startswith("╭─ message ") and top.endswith("╮"))
        self.assertTrue(bot.startswith("╰") and bot.endswith("sonnet · live ╯"))
        plain_top, plain_bot = box_frame(30)
        self.assertEqual(plain_top, "╭" + "─" * 28 + "╮")
        self.assertEqual(plain_bot, "╰" + "─" * 28 + "╯")

    def test_layout_compacts_on_short_terminals(self):
        self.assertEqual(layout(30), (26, False))
        self.assertEqual(layout(10), (6, False))
        self.assertEqual(layout(8), (6, True))

    def test_dialog_window_geometry(self):
        x, y, bw, bh, vis = dialog_window(30, 100, 9, "model",
                                          "enter pick · esc close", 30)
        self.assertEqual((bw, bh, vis), (36, 11, 9))
        self.assertEqual((x, y), ((100 - 36) // 2, (30 - 11) // 2))
        x2, _, bw2, bh2, vis2 = dialog_window(8, 30, 9, "model", "hint", 30)
        self.assertEqual((bw2, bh2, vis2), (28, 6, 4))


class TuiCommandTest(unittest.TestCase):
    """Slash commands run locally without network."""

    def _app(self):
        from claude_re.tui import App
        return App(None, "claude-sonnet-5-5", stealth=False)

    def test_unknown_command_becomes_note(self):
        app = self._app()
        self.assertFalse(app._command("/bogus"))
        self.assertEqual(app.messages[-1][0], "note")

    def test_new_clears_and_model_switches(self):
        app = self._app()
        app.messages.append(["user", "hi"])
        app._command("/new")
        self.assertEqual(app.messages, [])
        app._command("/model haiku")
        self.assertEqual(app.model, "claude-haiku-5-5")
        self.assertTrue(app.messages[-1][1].startswith("model:"))

    def test_quit_and_help(self):
        app = self._app()
        self.assertTrue(app._command("/quit"))
        self.assertFalse(app._command("/help"))
        self.assertEqual(app.dialog, "help")


if __name__ == "__main__":
    unittest.main()
