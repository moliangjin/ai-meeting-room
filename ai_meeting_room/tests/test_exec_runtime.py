from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_meeting_room.integrations.minimax_exec.adapter import MiniMaxExecAdapter
from ai_meeting_room.runtime.errors import RuntimeErrorCode, RuntimeFailure
from ai_meeting_room.runtime.events import extract_final_output, parse_stream_json_line
from ai_meeting_room.runtime.security import EnvironmentSanitizer, SideEffectPolicy
from ai_meeting_room.tasks.envelope import TaskEnvelope


class ExecRuntimeTests(unittest.TestCase):
    def test_envelope_is_bounded_and_serializable(self) -> None:
        envelope = TaskEnvelope("m", "t", "a", "WORKER", instruction="count files", previous_relevant_results=({"x": 1},))
        rendered = envelope.render_prompt()
        self.assertIn('"taskId":"t"', rendered)
        json.dumps(envelope.__dict__, default=list)

    def test_sanitizer_drops_unrelated_environment_and_hermes_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sanitizer = EnvironmentSanitizer(home=directory, safe_bin=Path(directory) / "safe-bin")
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "redacted-test", "REDFOX_API_KEY": "redacted-test"}):
                env = sanitizer.build_env(cwd=directory)
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("REDFOX_API_KEY", env)
            self.assertNotIn(".hermes/node/bin", env["PATH"])
            self.assertIn("AI_MEETING_ROOM_RUNTIME", env)

    def test_side_effect_policy_denies_unknown_and_external_commands(self) -> None:
        policy = SideEffectPolicy()
        self.assertFalse(policy.allows("unknown"))
        with self.assertRaises(PermissionError):
            policy.assert_local_command_allowed("lark-cli im --send message", ".")
        with self.assertRaises(PermissionError):
            policy.assert_local_command_allowed("curl https://example.invalid", ".")

    def test_stream_event_and_final_output(self) -> None:
        terminal = {"schemaVersion": 1, "sequence": 4, "timestampMs": 10, "runId": "run", "sessionId": "session", "turnId": "turn", "type": "exec.completed", "result": {"status": "succeeded", "output": "2 files"}}
        event = parse_stream_json_line(json.dumps(terminal))
        self.assertEqual(extract_final_output(event), "2 files")

    def test_unknown_fixture_is_fail_closed(self) -> None:
        with self.assertRaises(RuntimeFailure) as context:
            parse_stream_json_line(json.dumps({"schemaVersion": 1, "sequence": 1, "runId": "run", "type": "future.event"}))
        self.assertEqual(context.exception.code, RuntimeErrorCode.UNKNOWN)

    def test_adapter_command_never_uses_session_or_continue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = MiniMaxExecAdapter("minimax", directory, mcode_executable="/mcode", node_executable="/node", timeout_seconds=12, max_steps=3)
            adapter.start()
            class FakeProcess:
                pid = 123
                def __init__(self):
                    self.stdout = iter([])
                    self.stderr = None
                def poll(self): return 0
                def wait(self, timeout=None): return 0
            with patch("ai_meeting_room.integrations.minimax_exec.adapter.subprocess.Popen", return_value=FakeProcess()) as popen:
                adapter.send_task({"task_id": "task-1", "input": "read only"})
                command = popen.call_args.args[0]
            self.assertNotIn("--session", command)
            self.assertNotIn("--continue", command)
            self.assertIn("--output-format", command)
            self.assertIn("stream-json", command)


if __name__ == "__main__":
    unittest.main()
