import unittest
from datetime import datetime, timedelta, timezone

from phase0_poc.safety import (
    AgentHealth,
    AgentHeartbeat,
    CircuitState,
    DispatchBlockedError,
    GlobalCircuitBreaker,
    MeetingState,
)


class SafetyPocTests(unittest.TestCase):
    def test_fault_opens_pauses_and_interrupts_every_active_agent(self) -> None:
        interrupted: list[str] = []

        def interrupt(agent_id: str) -> None:
            interrupted.append(agent_id)

        breaker = GlobalCircuitBreaker(lambda: ("codex-1", "minimax-1"), interrupt)
        event = breaker.observe("minimax-1", AgentHealth.UNKNOWN, "provider heartbeat expired")

        self.assertTrue(event)
        self.assertEqual(breaker.state, CircuitState.OPEN)
        self.assertEqual(breaker.meeting_state, MeetingState.PAUSED)
        self.assertTrue(breaker.workspace_write_protected)
        self.assertTrue(breaker.stop_dispatch)
        self.assertEqual(interrupted, ["codex-1", "minimax-1"])
        self.assertEqual(breaker.last_event.event, "GLOBAL_PAUSE")
        self.assertEqual(breaker.last_event.trigger_agent_id, "minimax-1")

    def test_resume_requires_explicit_recovery_and_health(self) -> None:
        breaker = GlobalCircuitBreaker(lambda: (), lambda _: None)
        breaker.open("codex-1", "CLI exited")
        with self.assertRaises(RuntimeError):
            breaker.resume({"codex-1": AgentHealth.HEALTHY})
        breaker.begin_recovery()
        with self.assertRaises(ValueError):
            breaker.resume({"codex-1": AgentHealth.IDLE})
        self.assertTrue(breaker.stop_dispatch)
        breaker.resume({"codex-1": AgentHealth.HEALTHY})
        self.assertEqual(breaker.state, CircuitState.CLOSED)
        self.assertEqual(breaker.meeting_state, MeetingState.RUNNING)
        self.assertFalse(breaker.workspace_write_protected)
        self.assertFalse(breaker.stop_dispatch)

    def test_open_rejects_new_dispatch(self) -> None:
        breaker = GlobalCircuitBreaker(lambda: (), lambda _: None)
        breaker.open("codex-1", "real runtime status unavailable")
        with self.assertRaises(DispatchBlockedError):
            breaker.require_dispatch_allowed()

    def test_interrupt_failure_is_recorded(self) -> None:
        def interrupt(agent_id: str) -> None:
            if agent_id == "codex-1":
                raise TimeoutError("PTY did not close")

        breaker = GlobalCircuitBreaker(lambda: ("codex-1", "minimax-1"), interrupt)
        breaker.open("codex-1", "network failure")
        self.assertIn("codex-1", breaker.last_event.interrupt_errors)
        self.assertEqual(breaker.last_event.interrupted_agent_ids, ("codex-1", "minimax-1"))

    def test_open_emits_durable_snapshot(self) -> None:
        saved: list[dict[str, object]] = []
        breaker = GlobalCircuitBreaker(lambda: ("codex-1",), lambda _: None, persist_snapshot=saved.append)
        breaker.open("codex-1", "429 / quota unknown")
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["meetingState"], "PAUSED")
        self.assertTrue(saved[0]["workspaceWriteProtected"])
        self.assertEqual(saved[0]["triggerAgentId"], "codex-1")

    def test_heartbeat_uses_configured_timeout_and_tracks_required_fields(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        heartbeat = AgentHeartbeat(timeout_seconds=5, clock=lambda: start)
        record = heartbeat.beat("codex-1", "codex", AgentHealth.WORKING, "task-1")
        heartbeat.output("codex-1", start + timedelta(seconds=1))
        self.assertEqual(record.agent_id, "codex-1")
        self.assertEqual(record.provider, "codex")
        self.assertEqual(record.current_task_id, "task-1")
        self.assertEqual(record.last_output_at, start + timedelta(seconds=1))
        self.assertEqual(heartbeat.timed_out(start + timedelta(seconds=5)), ())
        self.assertEqual(heartbeat.timed_out(start + timedelta(seconds=5, microseconds=1)), ("codex-1",))


if __name__ == "__main__":
    unittest.main()
