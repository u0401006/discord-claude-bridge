"""
Smoke tests for bot.py pure functions.
Covers: chunk_text, _scan_fence, _validate_send_file, session persistence.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# ── bootstrap: set required env before importing bot ──────────────────────────

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("LOG_FILE", "/tmp/test_discord_bridge.log")
os.environ.setdefault("WORKING_DIR", "/tmp")

# Mock discord (may not be on PYTHONPATH outside the venv)
if "discord" not in sys.modules:
    _discord = MagicMock()
    _discord.Intents.default.return_value = MagicMock()
    _discord.Client.return_value = MagicMock()
    sys.modules["discord"] = _discord
    sys.modules["discord.ext"] = MagicMock()

# Mock dotenv in case it's missing
if "dotenv" not in sys.modules:
    _dotenv = MagicMock()
    _dotenv.load_dotenv = lambda *a, **kw: None
    sys.modules["dotenv"] = _dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402

# ── _scan_fence ───────────────────────────────────────────────────────────────


class TestScanFence(unittest.TestCase):
    def test_no_fence(self):
        self.assertIsNone(bot._scan_fence("hello\nno fences"))

    def test_open_plain_fence(self):
        self.assertEqual(bot._scan_fence("```\nsome code"), "")

    def test_open_lang_fence(self):
        self.assertEqual(bot._scan_fence("```python\ncode"), "python")

    def test_closed_fence(self):
        self.assertIsNone(bot._scan_fence("```python\ncode\n```"))

    def test_two_closed_fences(self):
        self.assertIsNone(bot._scan_fence("```\na\n```\n\n```\nb\n```"))

    def test_unclosed_second_fence(self):
        self.assertEqual(bot._scan_fence("```\na\n```\n\n```go\nb"), "go")


# ── chunk_text ────────────────────────────────────────────────────────────────


class TestChunkText(unittest.TestCase):
    LIMIT = 200

    def _assert_chunks_valid(self, chunks: list[str]) -> None:
        """Every chunk fits in LIMIT and leaves no open fence."""
        for i, c in enumerate(chunks):
            self.assertLessEqual(len(c), self.LIMIT, f"chunk {i} too long ({len(c)})")
            self.assertIsNone(bot._scan_fence(c), f"chunk {i} has unclosed fence")

    def test_short_text_unchanged(self):
        text = "hello world"
        self.assertEqual(bot.chunk_text(text, self.LIMIT), [text])

    def test_plain_long_text_splits(self):
        text = "word " * 60  # 300 chars
        chunks = bot.chunk_text(text, self.LIMIT)
        self.assertGreater(len(chunks), 1)
        self._assert_chunks_valid(chunks)

    def test_fenced_code_each_chunk_balanced(self):
        code = "x = 1\n" * 50  # ~300 chars; total > LIMIT=200
        text = f"```python\n{code}```"
        chunks = bot.chunk_text(text, self.LIMIT)
        self.assertGreater(len(chunks), 1)
        self._assert_chunks_valid(chunks)

    def test_regression_2008_char_fence(self):
        """Evaluator regression: 2008-char fenced block with DISCORD_CHUNK limit."""
        code = "x" * 1998
        text = f"```\n{code}\n```"
        chunks = bot.chunk_text(text, bot.DISCORD_CHUNK)
        self.assertGreater(len(chunks), 1)
        for i, c in enumerate(chunks):
            self.assertLessEqual(len(c), bot.DISCORD_CHUNK, f"chunk {i} too long")
            self.assertIsNone(bot._scan_fence(c), f"chunk {i} has unclosed fence")

    def test_text_with_no_newlines_splits(self):
        """Hard-cut path: single very long line."""
        text = "a" * 500
        chunks = bot.chunk_text(text, self.LIMIT)
        for c in chunks:
            self.assertLessEqual(len(c), self.LIMIT)


# ── _validate_send_file ───────────────────────────────────────────────────────


class TestValidateSendFile(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.safe_txt = os.path.join(self.tmpdir, "output.txt")
        with open(self.safe_txt, "w") as f:
            f.write("data")

    def _with_dirs(self):
        return patch.object(bot, "_SEND_FILE_DIRS", [os.path.realpath(self.tmpdir)])

    def test_valid_file_passes(self):
        with self._with_dirs():
            result = bot._validate_send_file(self.safe_txt)
        self.assertEqual(result, os.path.realpath(self.safe_txt))

    def test_path_traversal_rejected(self):
        evil = os.path.join(self.tmpdir, "../../etc/hosts")
        with self._with_dirs():
            self.assertIsNone(bot._validate_send_file(evil))

    def test_etc_hosts_rejected(self):
        with self._with_dirs():
            self.assertIsNone(bot._validate_send_file("/etc/hosts"))

    def test_ssh_key_rejected(self):
        with self._with_dirs():
            self.assertIsNone(bot._validate_send_file("~/.ssh/id_rsa"))

    def test_disallowed_extension_rejected(self):
        bad = os.path.join(self.tmpdir, "script.exe")
        with open(bad, "w") as f:
            f.write("bad")
        with self._with_dirs():
            self.assertIsNone(bot._validate_send_file(bad))

    def test_nonexistent_file_rejected(self):
        ghost = os.path.join(self.tmpdir, "ghost.txt")
        with self._with_dirs():
            self.assertIsNone(bot._validate_send_file(ghost))


# ── session persistence ───────────────────────────────────────────────────────


class TestSessionPersistence(unittest.TestCase):
    def setUp(self):
        self._orig_file = bot.SESSIONS_FILE
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        self.tmp_file = tmp.name
        bot.SESSIONS_FILE = self.tmp_file

    def tearDown(self):
        bot.SESSIONS_FILE = self._orig_file
        os.unlink(self.tmp_file)

    def test_save_load_roundtrip(self):
        bot._sessions["ch1_u1"] = "sess-abc"
        bot._turn_counts["ch1_u1"] = 7
        bot._stopped_sessions.add("ch1_u2")
        bot._save_sessions()

        sessions, turns, stopped = bot._load_sessions()
        self.assertEqual(sessions.get("ch1_u1"), "sess-abc")
        self.assertEqual(turns.get("ch1_u1"), 7)
        self.assertIn("ch1_u2", stopped)

    def test_legacy_format_backwards_compat(self):
        with open(self.tmp_file, "w") as f:
            json.dump({"ch1_u1": "old-id"}, f)

        sessions, turns, stopped = bot._load_sessions()
        self.assertEqual(sessions["ch1_u1"], "old-id")
        self.assertEqual(turns, {})
        self.assertEqual(stopped, set())

    def test_empty_file_returns_defaults(self):
        with open(self.tmp_file, "w") as f:
            f.write("")

        sessions, turns, stopped = bot._load_sessions()
        self.assertEqual(sessions, {})
        self.assertEqual(turns, {})
        self.assertEqual(stopped, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
