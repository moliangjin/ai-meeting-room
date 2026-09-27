from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_meeting_room.integrations.minimax_sandbox.runtime import MiniMaxSandboxRuntime
from ai_meeting_room.runtime.egress import EgressGateway, EgressRule
from ai_meeting_room.runtime.errors import SecurityPolicyViolation
from ai_meeting_room.runtime.sandbox import DockerSandboxRuntime, SandboxSpec, SandboxState, SandboxUnavailable


class HealthyGateway(EgressGateway):
    def health_check(self) -> bool:
        return True


class SandboxRuntimeTests(unittest.TestCase):
    def make_runtime(self, directory: str, gateway=None) -> DockerSandboxRuntime:
        return DockerSandboxRuntime(SandboxSpec("meeting-minimax", "image", directory, "minimax-state", "meeting-egress", image_digest="sha256:" + "a" * 64), gateway or HealthyGateway())

    def test_run_command_has_hardening_and_narrow_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            command = self.make_runtime(directory).build_run_command()
        self.assertIn("--read-only", command)
        self.assertIn("--cap-drop", command)
        self.assertIn("ALL", command)
        self.assertIn("no-new-privileges", command)
        self.assertNotIn("--privileged", command)
        self.assertNotIn("--network=host", command)
        self.assertFalse(any("docker.sock" in item or item == "/Users" for item in command))
        self.assertTrue(any("dst=/workspace" in item and "type=bind" in item for item in command))
        self.assertTrue(any("dst=/home/mcode/.minimax" in item and "type=volume" in item for item in command))
        self.assertTrue(any(item.startswith("/home/mcode:rw") and "mode=1777" in item for item in command))
        self.assertIn("MINIMAX_DATA_DIR=/home/mcode/.minimax", command)
        self.assertIn("MAVIS_DATA_DIR=/home/mcode/.minimax", command)
        self.assertIn("image@sha256:" + "a" * 64, command)
        self.assertNotIn("NODE_USE_ENV_PROXY=1", command)

    def test_unpinned_image_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = DockerSandboxRuntime(SandboxSpec("x", "image", directory, "vol", "net"), HealthyGateway())
            with self.assertRaises(ValueError):
                runtime.build_run_command()

    def test_gateway_failure_blocks_start_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.make_runtime(directory, EgressGateway())
            with self.assertRaises(SandboxUnavailable):
                runtime.start()
            self.assertEqual(runtime.state, SandboxState.UNKNOWN)

    def test_start_requires_daemon_and_never_runs_host_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.make_runtime(directory)
            calls = []
            def fake_docker(args, **kwargs):
                calls.append(list(args))
                if args[:1] == ["inspect"]:
                    return subprocess.CompletedProcess(args, 1, "", "not found")
                return subprocess.CompletedProcess(args, 0, "", "")
            with patch.object(runtime, "_docker", side_effect=fake_docker):
                runtime.start()
            self.assertEqual(runtime.state, SandboxState.READY)
            self.assertEqual(calls[0], ["info"])
            self.assertEqual(calls[1], ["inspect", "meeting-minimax"])
            self.assertEqual(calls[2][0], "run")

    def test_existing_stopped_sandbox_is_restarted_for_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.make_runtime(directory)
            calls = []
            def fake_docker(args, **kwargs):
                calls.append(list(args))
                if args[:1] == ["inspect"]:
                    return subprocess.CompletedProcess(args, 0, "{}", "")
                return subprocess.CompletedProcess(args, 0, "", "")
            with patch.object(runtime, "_docker", side_effect=fake_docker):
                runtime.start()
            self.assertEqual(calls[2], ["start", "meeting-minimax"])

    def test_unexpected_container_stop_is_unknown_not_idle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.make_runtime(directory)
            with patch.object(runtime, "_docker", return_value=subprocess.CompletedProcess(["inspect"], 0, "false", "")):
                self.assertEqual(runtime.get_status(), SandboxState.UNKNOWN.value)

    def test_minimax_command_is_fresh_exec_without_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = MiniMaxSandboxRuntime(SandboxSpec("x", "image", directory, "vol", "net", image_digest="sha256:" + "b" * 64), HealthyGateway())
            command = runtime.build_mcode_command("read only")
            run_command = runtime.build_run_command()
        self.assertEqual(command[:2], ["mcode", "exec"])
        self.assertNotIn("--session", command)
        self.assertNotIn("--continue", command)
        self.assertIn("stream-json", command)
        self.assertIn("NODE_USE_ENV_PROXY=1", run_command)

    def test_egress_is_exact_and_audited(self) -> None:
        events = []
        gateway = EgressGateway((EgressRule("api.minimax.example", (443,), ("https",), "approved test endpoint"),), events.append)
        self.assertFalse(gateway.decide("a", "evil.example", 443))
        self.assertTrue(gateway.decide("a", "api.minimax.example", 443))
        self.assertEqual([event.decision for event in events], ["DENY", "ALLOW"])
        self.assertFalse(gateway.policy_document()["directInternetRoute"])

    def test_security_violation_is_distinct_error(self) -> None:
        with self.assertRaises(SecurityPolicyViolation):
            from ai_meeting_room.runtime.security import SideEffectPolicy
            SideEffectPolicy().assert_local_command_allowed("curl https://example.invalid", ".")


if __name__ == "__main__":
    unittest.main()
