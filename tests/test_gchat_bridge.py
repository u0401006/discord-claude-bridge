"""
Unit tests for gchat_bridge.py pure helpers (no google packages required —
they are lazily imported only inside the Pub/Sub / Chat API paths).
Covers: session_key_for, markdown_to_gchat, is_duplicate, extract_prompt.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("LOG_FILE", "/tmp/test_gchat_bridge.log")

# Mock dotenv in case it's missing
if "dotenv" not in sys.modules:
    _dotenv = MagicMock()
    _dotenv.load_dotenv = lambda *a, **kw: None
    sys.modules["dotenv"] = _dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gchat_bridge as gb  # noqa: E402


def _event(space="spaces/AAA", thread="spaces/AAA/threads/TTT",
           threading_state="THREADED_MESSAGES", text="hello",
           argument_text=None, msg_name="spaces/AAA/messages/M1",
           user="users/111", display="Alice"):
    return {
        "type": "MESSAGE",
        "space": {"name": space, "spaceThreadingState": threading_state},
        "user": {"name": user, "displayName": display},
        "message": {
            "name": msg_name,
            "text": text,
            "argumentText": argument_text,
            "thread": {"name": thread},
        },
    }


class TestSessionKey(unittest.TestCase):
    def test_auto_threaded_space_uses_thread(self):
        with patch.object(gb, "GCHAT_SESSION_SCOPE", "auto"):
            key = gb.session_key_for(_event())
        self.assertEqual(key, "gthTTT")

    def test_auto_flat_space_uses_space(self):
        """Flat spaces/DMs mint a thread per message — key must stay stable."""
        with patch.object(gb, "GCHAT_SESSION_SCOPE", "auto"):
            k1 = gb.session_key_for(
                _event(threading_state="FLAT_MESSAGES", thread="spaces/AAA/threads/T1"))
            k2 = gb.session_key_for(
                _event(threading_state="FLAT_MESSAGES", thread="spaces/AAA/threads/T2"))
        self.assertEqual(k1, "gspAAA")
        self.assertEqual(k1, k2)

    def test_thread_scope_shared_by_users(self):
        with patch.object(gb, "GCHAT_SESSION_SCOPE", "thread"):
            k1 = gb.session_key_for(_event(user="users/111"))
            k2 = gb.session_key_for(_event(user="users/222"))
        self.assertEqual(k1, k2)

    def test_space_scope(self):
        with patch.object(gb, "GCHAT_SESSION_SCOPE", "space"):
            self.assertEqual(gb.session_key_for(_event()), "gspAAA")

    def test_missing_thread_falls_back_to_space(self):
        ev = _event()
        ev["message"]["thread"] = {}
        with patch.object(gb, "GCHAT_SESSION_SCOPE", "thread"):
            self.assertEqual(gb.session_key_for(ev), "gspAAA")


class TestMarkdownToGchat(unittest.TestCase):
    def test_bold(self):
        self.assertEqual(gb.markdown_to_gchat("**bold** text"), "*bold* text")

    def test_link(self):
        self.assertEqual(
            gb.markdown_to_gchat("see [docs](https://example.com/a)"),
            "see <https://example.com/a|docs>",
        )

    def test_heading(self):
        self.assertEqual(gb.markdown_to_gchat("## Title\nbody"), "*Title*\nbody")

    def test_strikethrough(self):
        self.assertEqual(gb.markdown_to_gchat("~~gone~~"), "~gone~")

    def test_code_fence_untouched(self):
        text = "before\n```python\nx = '**not bold**'\n```\nafter **b**"
        out = gb.markdown_to_gchat(text)
        self.assertIn("x = '**not bold**'", out)
        self.assertIn("after *b*", out)

    def test_inline_code_untouched(self):
        self.assertEqual(gb.markdown_to_gchat("`**raw**` and **b**"), "`**raw**` and *b*")


class TestDedup(unittest.TestCase):
    def setUp(self):
        gb._seen_messages.clear()

    def test_first_seen_then_duplicate(self):
        self.assertFalse(gb.is_duplicate("m1"))
        self.assertTrue(gb.is_duplicate("m1"))

    def test_empty_name_never_duplicate(self):
        self.assertFalse(gb.is_duplicate(""))
        self.assertFalse(gb.is_duplicate(""))

    def test_lru_eviction(self):
        with patch.object(gb, "_SEEN_MAX", 3):
            for i in range(4):
                gb.is_duplicate(f"m{i}")
        self.assertFalse(gb.is_duplicate("m0"))  # evicted → treated as new


class TestSessionInstruction(unittest.TestCase):
    def test_contains_space_and_thread(self):
        with patch.object(gb, "GCHAT_MCP_CONTEXT_HINT", True):
            instr = gb._make_session_instruction("spaces/AAA", "spaces/AAA/threads/TTT")
        self.assertIn("spaces/AAA", instr)
        self.assertIn("spaces/AAA/threads/TTT", instr)
        self.assertIn("list_messages", instr)

    def test_no_thread_targets_space(self):
        with patch.object(gb, "GCHAT_MCP_CONTEXT_HINT", True):
            instr = gb._make_session_instruction("spaces/AAA", None)
        self.assertIn("list_messages for spaces/AAA", instr)

    def test_disabled_returns_empty(self):
        with patch.object(gb, "GCHAT_MCP_CONTEXT_HINT", False):
            self.assertEqual(gb._make_session_instruction("spaces/AAA", None), "")

    def test_forbids_mcp_send(self):
        """The hint must stop the backend replying via MCP as the human user."""
        with patch.object(gb, "GCHAT_MCP_CONTEXT_HINT", True):
            instr = gb._make_session_instruction("spaces/AAA", None)
        self.assertIn("Never use an MCP send_message", instr)


class TestExtractPrompt(unittest.TestCase):
    def test_argument_text_preferred(self):
        ev = _event(text="@bot do this", argument_text="  do this  ")
        self.assertEqual(gb.extract_prompt(ev), "do this")

    def test_falls_back_to_text(self):
        ev = _event(text="direct message", argument_text=None)
        self.assertEqual(gb.extract_prompt(ev), "direct message")


if __name__ == "__main__":
    unittest.main(verbosity=2)
