from datetime import datetime, timezone
import unittest
from pathlib import Path

from ate_integration import instruction, message_context


class AteIntegrationTests(unittest.TestCase):
    def test_disabled_has_no_prompt(self):
        self.assertEqual(instruction(""), "")

    def test_command_quotes_spaces(self):
        hint = instruction("/tmp/food tools")
        self.assertIn("'" + str(Path("/tmp/food tools").resolve() / ".venv/bin/python") + "'", hint)
        self.assertIn("AGENT.md", hint)

    def test_message_id_and_taipei_date(self):
        context = message_context(123, 456, datetime(2026, 9, 27, 17, tzinfo=timezone.utc))
        self.assertIn('"message_id": "123"', context)
        self.assertIn('"date": "2026-09-28"', context)


if __name__ == "__main__":
    unittest.main()
