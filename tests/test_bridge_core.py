"""
Tests for bridge_core streaming + cancellation, using fake backend scripts
run through the real subprocess path (no claude CLI required).
"""

import json
import os
import sys
import tempfile
import textwrap
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge_core  # noqa: E402


def _write_script(body: str) -> str:
    """Write a python script acting as a fake backend (invoked as base_args[0])."""
    f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
    f.write(textwrap.dedent(body))
    f.close()
    return f.name


class TestDescribeStreamEvent(unittest.TestCase):
    def test_tool_use_with_file_path(self):
        event = {
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "thinking..."},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "bot.py"}},
            ]},
        }
        self.assertEqual(bridge_core._describe_stream_event(event), ["Read: bot.py"])

    def test_description_preferred_and_truncated(self):
        event = {
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"description": "x" * 200, "command": "ls"}},
            ]},
        }
        (desc,) = bridge_core._describe_stream_event(event)
        self.assertTrue(desc.startswith("Bash: xxx"))
        self.assertLessEqual(len(desc), len("Bash: ") + 80)

    def test_non_assistant_ignored(self):
        self.assertEqual(bridge_core._describe_stream_event({"type": "user"}), [])


class TestStreaming(unittest.IsolatedAsyncioTestCase):
    async def test_events_and_final_result(self):
        script = _write_script("""\
            import json
            print(json.dumps({"type": "system", "subtype": "init"}))
            print(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]}}))
            print(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}}]}}))
            print(json.dumps({"type": "result", "result": "all done", "session_id": "s-123"}))
        """)
        try:
            events = []

            async def on_p(d):
                events.append(d)

            reply = await bridge_core.run_backend_streaming(
                "hi", backend_bin=sys.executable, base_args=[script],
                timeout=15, on_progress=on_p,
            )
            self.assertTrue(reply.ok)
            self.assertEqual(reply.text, "all done")
            self.assertEqual(reply.session_id, "s-123")
            self.assertEqual(events, ["Read: a.py", "Bash: pytest"])
        finally:
            os.unlink(script)

    async def test_adapter_single_json_is_compatible(self):
        """Adapters ignore stream-json and print one object — must still work."""
        script = _write_script("""\
            import json
            print(json.dumps({"result": "adapter ok", "session_id": "a-1"}))
        """)
        try:
            reply = await bridge_core.run_backend_streaming(
                "hi", backend_bin=sys.executable, base_args=[script], timeout=15,
            )
            self.assertTrue(reply.ok)
            self.assertEqual(reply.text, "adapter ok")
            self.assertEqual(reply.session_id, "a-1")
        finally:
            os.unlink(script)

    async def test_stale_session_detected(self):
        script = _write_script("""\
            import sys
            sys.stderr.write("No conversation found with session ID x")
            sys.exit(1)
        """)
        try:
            reply = await bridge_core.run_backend_streaming(
                "hi", backend_bin=sys.executable, base_args=[script],
                timeout=15, resume="dead-session",
            )
            self.assertTrue(reply.stale_session)
        finally:
            os.unlink(script)

    async def test_cancel_kills_inflight_run(self):
        script = _write_script("""\
            import time
            time.sleep(30)
        """)
        try:
            import asyncio

            task = asyncio.create_task(bridge_core.run_backend_streaming(
                "hi", backend_bin=sys.executable, base_args=[script],
                timeout=60, proc_key="k1",
            ))
            # wait until the proc registers, then cancel it
            for _ in range(100):
                if "k1" in bridge_core._active_procs:
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(bridge_core.cancel_backend("k1"))
            reply = await asyncio.wait_for(task, timeout=10)
            self.assertTrue(reply.cancelled)
            self.assertNotIn("k1", bridge_core._active_procs)
        finally:
            os.unlink(script)


class TestCancelBackend(unittest.TestCase):
    def test_unknown_key_returns_false(self):
        self.assertFalse(bridge_core.cancel_backend("nope"))


class TestBuildBackendEnv(unittest.TestCase):
    def test_frontend_secrets_stripped(self):
        with unittest.mock.patch.dict(os.environ, {
            "DISCORD_TOKEN": "secret", "OPENAI_API_KEY": "sk-x", "PATH": "/usr/bin",
        }):
            env = bridge_core.build_backend_env()
        self.assertNotIn("DISCORD_TOKEN", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertIn("PATH", env)  # normal vars pass through

    def test_extra_pass_reallows(self):
        """openai-adapter backend needs its key: BACKEND_ENV_PASS=OPENAI_API_KEY."""
        with unittest.mock.patch.dict(os.environ, {
            "DISCORD_TOKEN": "secret", "OPENAI_API_KEY": "sk-x",
        }):
            env = bridge_core.build_backend_env(extra_pass="OPENAI_API_KEY")
        self.assertIn("OPENAI_API_KEY", env)
        self.assertNotIn("DISCORD_TOKEN", env)

    def test_extra_deny(self):
        with unittest.mock.patch.dict(os.environ, {"MY_SECRET": "x"}):
            env = bridge_core.build_backend_env(extra_deny="MY_SECRET")
        self.assertNotIn("MY_SECRET", env)


class TestWsDirective(unittest.TestCase):
    def test_parse_with_task(self):
        content, ws = bridge_core.parse_ws_directive("[[ws:~/proj]] fix the bug")
        self.assertEqual(content, "fix the bug")
        self.assertEqual(ws, "~/proj")

    def test_parse_directive_only(self):
        content, ws = bridge_core.parse_ws_directive("[[ws:/tmp/x]]")
        self.assertEqual(content, "")
        self.assertEqual(ws, "/tmp/x")

    def test_no_directive(self):
        content, ws = bridge_core.parse_ws_directive("hello")
        self.assertEqual(content, "hello")
        self.assertIsNone(ws)


class TestValidateWorkdir(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.sub = os.path.join(self.root, "proj")
        os.mkdir(self.sub)

    def test_dir_inside_root_ok(self):
        self.assertEqual(
            bridge_core.validate_workdir(self.sub, [self.root]),
            os.path.realpath(self.sub),
        )

    def test_traversal_rejected(self):
        evil = os.path.join(self.sub, "..", "..")
        self.assertIsNone(bridge_core.validate_workdir(evil, [self.root]))

    def test_outside_root_rejected(self):
        self.assertIsNone(bridge_core.validate_workdir("/etc", [self.root]))

    def test_nonexistent_rejected(self):
        ghost = os.path.join(self.root, "nope")
        self.assertIsNone(bridge_core.validate_workdir(ghost, [self.root]))

    def test_symlink_escape_rejected(self):
        link = os.path.join(self.root, "link")
        os.symlink("/etc", link)
        self.assertIsNone(bridge_core.validate_workdir(link, [self.root]))


class TestRunBackendCancel(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_non_streaming_run(self):
        script = _write_script("""\
            import time
            time.sleep(30)
        """)
        try:
            import asyncio

            task = asyncio.create_task(bridge_core.run_backend(
                "hi", backend_bin=sys.executable, base_args=[script],
                timeout=60, proc_key="k2",
            ))
            for _ in range(100):
                if "k2" in bridge_core._active_procs:
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(bridge_core.cancel_backend("k2"))
            reply = await asyncio.wait_for(task, timeout=10)
            self.assertTrue(reply.cancelled)
        finally:
            os.unlink(script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
