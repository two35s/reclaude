"""Small regression tests for the risky, parser-heavy logic.

Run from the repo root:  .venv/bin/python -m unittest discover -s tests -v
"""
import unittest
from unittest import mock

from claude_re import chrome_cookies
from claude_re.config import MODEL_ALIASES, PAYLOAD_TEMPLATE, resolve_model
from claude_re.payload import build_payload
from claude_re.streaming import _sse_data, _split_lines
from claude_re.tui import App, Chat, Msg, block_word, render_messages, wrap


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


class TuiRenderTest(unittest.TestCase):
    """The chat view must stay within width and mark roles/streaming."""

    def test_wrap_never_exceeds_width(self):
        for width in (1, 5, 20):
            for line in wrap("the quick brown fox\nsupercalifragilisticexpialidocious", width):
                self.assertLessEqual(len(line), width)

    def test_render_marks_roles_and_streaming_cursor(self):
        chat = Chat("claude-sonnet-5-5")
        chat.messages.append(Msg("user", "hi"))
        chat.messages.append(Msg("assistant", "hello", streaming=True))
        text = "\n".join(line for line, _ in render_messages(chat, 40))
        self.assertIn("┃ hi", text)
        self.assertIn("hello▌", text)


class TuiLayoutTest(unittest.TestCase):
    """The empty state floats the composer; a live chat pins it to the bottom."""

    def test_composer_centred_when_empty_pinned_when_live(self):
        app = App(None, "claude-sonnet-5-5", stealth=False)
        empty, comp, foot = app._layout(40)
        self.assertTrue(empty)
        self.assertLess(comp, 40 - 7)
        app.chat().messages.append(Msg("user", "hi"))
        live, comp2, foot2 = app._layout(40)
        self.assertFalse(live)
        self.assertEqual((comp2, foot2), (33, 38))

    def test_block_word_rows_are_rectangular(self):
        rows = block_word("reclaude")
        self.assertEqual(len(rows), 3)
        self.assertEqual({len(r) for r in rows}, {31})

    def test_content_column_centres_and_clamps(self):
        app = App(None, "claude-sonnet-5-5", stealth=False)
        x, cw = app.col(130)
        self.assertEqual(cw, 76)
        self.assertEqual(x, (130 - 76) // 2)
        x2, cw2 = app.col(50)
        self.assertEqual((x2, cw2), (0, 50))


if __name__ == "__main__":
    unittest.main()
