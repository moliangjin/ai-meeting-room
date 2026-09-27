from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from ai_meeting_room.core.heartbeat import AgentHeartbeat
from ai_meeting_room.core.safety import CircuitState, GlobalCircuitBreaker, SafetyEngine
from ai_meeting_room.models import AgentHealth, MeetingStatus
from ai_meeting_room.persistence.sqlite_store import SQLiteStore
from ai_meeting_room.product.app import Phase2Application
from test_phase2_product import FastCodexAdapter


class _Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FatalWatcherError(BaseException):
    pass


class _HealthyResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class SprintAHeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.adapters: list[FastCodexAdapter] = []

        def adapter_factory(**_kwargs):
            adapter = FastCodexAdapter()
            self.adapters.append(adapter)
            return adapter

        self.app = Phase2Application(
            SQLiteStore(root / "state.db"),
            adapter_factory=adapter_factory,
            heartbeat_timeout_seconds=1.0,
            monitor_poll_interval_seconds=0.2,
        )
        self.meeting_id = self.app.create_meeting("Sprint A T3", str(root))["meeting"]["meeting_id"]
        self.agent_id = self.app.join_provider(self.meeting_id, "codex")["agents"][0]["agent_id"]
        self.core = self.app._core(self.meeting_id)
        self.core.mark_ready()
        self.core.start()

    def tearDown(self) -> None:
        self.app.close()
        self.temp.cleanup()

    def use_clock(self) -> _Clock:
        clock = _Clock()
        self.core.heartbeat._clock = clock
        self.core.heartbeat.timeout_seconds = self.app.heartbeat_timeout_seconds
        self.core.beat_agent(self.agent_id, AgentHealth.HEALTHY)
        return clock

    def test_01_heartbeat_record_and_event_keep_provider_runtime_identity(self) -> None:
        record = self.core.heartbeat.get(self.agent_id)
        binding = self.core.runtime.current_binding(self.agent_id)
        event = next(e for e in reversed(self.core.events.history()) if e.event_type == "AgentHeartbeatReceived")

        self.assertEqual(record.provider, "codex")
        self.assertEqual(record.runtime_generation, binding.generation)
        self.assertEqual(record.terminal_id, binding.terminal_id)
        self.assertEqual(event.payload["provider"], "codex")
        self.assertEqual(event.payload["runtimeGeneration"], binding.generation)

    def test_02_monotonic_timeout_ignores_wall_clock_adjustment(self) -> None:
        clock = _Clock()
        heartbeat = AgentHeartbeat("m1", 1.0, self.core.events, clock=clock)
        heartbeat.beat("a1", "codex", AgentHealth.HEALTHY, runtime_generation=3, terminal_id="t3")

        self.assertEqual(heartbeat.timed_out(), ())
        clock.advance(0.9)
        self.assertEqual(heartbeat.timed_out(), ())
        clock.advance(0.2)
        self.assertEqual(heartbeat.timed_out(), ("a1",))
        self.assertEqual(heartbeat.timed_out(now="1900-01-01T00:00:00+00:00"), ())

    def test_03_joined_stale_heartbeat_triggers_safety_pause(self) -> None:
        clock = self.use_clock()
        self.app._poll_agent = lambda _core, _agent_id: AgentHealth.HEALTHY
        clock.advance(1.21)  # configured 1.0s timeout + 0.2s poll grace

        self.app._monitor_once(self.meeting_id)

        snapshot = self.app.snapshot(self.meeting_id)
        self.assertEqual(snapshot["meeting"]["status"], "PAUSED")
        self.assertEqual(snapshot["circuit"]["circuitState"], "OPEN")
        self.assertTrue(snapshot["circuit"]["stopDispatch"])
        self.assertTrue(snapshot["circuit"]["workspaceWriteProtected"])
        self.assertEqual(snapshot["circuit"]["triggerAgentId"], self.agent_id)
        self.assertEqual(self.core.registry.get(self.agent_id).health, AgentHealth.LOST)

    def test_04_stale_unjoined_provider_does_not_pause_meeting(self) -> None:
        clock = self.use_clock()
        self.core.heartbeat.beat("unjoined-minimax", "minimax", AgentHealth.UNKNOWN)
        self.core.beat_agent(self.agent_id, AgentHealth.HEALTHY)
        clock.advance(1.21)
        # Make only the non-member heartbeat stale; the joined Agent is refreshed at the current tick.
        self.core.beat_agent(self.agent_id, AgentHealth.HEALTHY)
        clock.advance(1.21)
        self.core.beat_agent(self.agent_id, AgentHealth.HEALTHY)
        self.app._poll_agent = lambda _core, _agent_id: AgentHealth.HEALTHY

        self.app._monitor_once(self.meeting_id)

        self.assertEqual(self.core.meeting.meeting.status, MeetingStatus.RUNNING)
        self.assertEqual(self.core.safety.breaker.state, CircuitState.CLOSED)

    def test_05_polling_delay_within_configured_grace_does_not_false_timeout(self) -> None:
        clock = self.use_clock()
        clock.advance(1.05)  # later than heartbeat threshold, still inside timeout + poll interval

        self.app._monitor_once(self.meeting_id)

        self.assertEqual(self.core.meeting.meeting.status, MeetingStatus.RUNNING)
        self.assertEqual(self.core.safety.breaker.state, CircuitState.CLOSED)
        self.assertEqual(self.core.heartbeat.timed_out(grace_seconds=0.2), ())

    def test_06_one_provider_poll_exception_isolated_and_monitor_survives(self) -> None:
        first_call = threading.Event()

        def faulty_poll(_core, agent_id):
            if agent_id == self.agent_id:
                first_call.set()
                raise RuntimeError("provider poll failed")
            return AgentHealth.HEALTHY

        self.app._poll_agent = faulty_poll
        self.app._start_monitor(self.meeting_id)
        thread = self.app._monitor_threads[self.meeting_id]

        self.assertTrue(first_call.wait(1.0))
        time.sleep(0.05)
        self.assertTrue(thread.is_alive())
        self.assertEqual(self.core.meeting.meeting.status, MeetingStatus.PAUSED)
        self.assertEqual(self.core.safety.breaker.state, CircuitState.OPEN)

    def test_07_fatal_monitor_failure_routes_through_safety_and_fails_closed(self) -> None:
        def fatal(_meeting_id):
            raise _FatalWatcherError("monitor invariant failed")

        self.app._monitor_once = fatal
        self.app._start_monitor(self.meeting_id)
        thread = self.app._monitor_threads[self.meeting_id]
        thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(self.core.safety.breaker.state, CircuitState.OPEN)
        self.assertTrue(self.core.safety.breaker.stop_dispatch)
        self.assertTrue(self.core.safety.breaker.workspace_write_protected)
        self.assertEqual(self.core.meeting.meeting.status, MeetingStatus.PAUSED)
        self.assertEqual(self.core.safety.breaker.participant_type, "SYSTEM")

    def test_08_duplicate_start_and_recovery_reentry_keep_single_monitor(self) -> None:
        self.app._monitor_once = lambda _meeting_id: None
        self.app._start_monitor(self.meeting_id)
        original_thread = self.app._monitor_threads[self.meeting_id]
        self.app._start_monitor(self.meeting_id)
        self.assertIs(self.app._monitor_threads[self.meeting_id], original_thread)

        self.app.pause(self.meeting_id, "reentry test")
        from unittest.mock import patch

        with patch("ai_meeting_room.product.app.urllib.request.urlopen", return_value=_HealthyResponse()):
            result = self.app.recover(self.meeting_id)

        self.assertTrue(result["recovered"])
        self.assertTrue(original_thread.is_alive())
        self.assertIs(self.app._monitor_threads[self.meeting_id], original_thread)

    def test_09_heartbeat_returning_never_auto_resumes_paused_meeting(self) -> None:
        self.app.pause(self.meeting_id, "heartbeat fault")
        self.core.beat_agent(self.agent_id, AgentHealth.HEALTHY)

        self.assertEqual(self.core.meeting.meeting.status, MeetingStatus.PAUSED)
        self.assertEqual(self.core.safety.breaker.state, CircuitState.OPEN)
        self.assertTrue(self.core.safety.breaker.stop_dispatch)

    def test_10_controlled_recovery_refreshes_heartbeat_before_monitor_reentry(self) -> None:
        clock = self.use_clock()
        self.app._monitor_once = lambda _meeting_id: None
        self.app.pause(self.meeting_id, "long pause")
        clock.advance(10.0)

        self.assertTrue(self.core.recover(
            cao_health_check=lambda: True,
            workspace_health_check=lambda: True,
            brain_health_check=lambda: True,
        ))

        record = self.core.heartbeat.get(self.agent_id)
        self.assertEqual(record.last_seen_monotonic, clock.now)
        self.assertEqual(self.core.meeting.meeting.status, MeetingStatus.RUNNING)
        self.assertEqual(self.core.safety.breaker.state, CircuitState.CLOSED)
        self.assertEqual(self.core.heartbeat.timed_out(grace_seconds=0.2), ())

    def test_11_safety_engine_uses_formal_core_constructor(self) -> None:
        self.assertIsInstance(self.core.safety, SafetyEngine)
        self.assertIsInstance(self.core.safety.breaker, GlobalCircuitBreaker)
        self.assertEqual(self.core.safety.breaker.state, CircuitState.CLOSED)
        self.assertEqual(self.core.heartbeat.timeout_seconds, 1.0)
        self.assertEqual(self.app.monitor_poll_interval_seconds, 0.2)

    def test_12_close_stops_existing_monitor_thread(self) -> None:
        self.app._monitor_once = lambda _meeting_id: None
        self.app._start_monitor(self.meeting_id)
        thread = self.app._monitor_threads[self.meeting_id]

        self.app.close()

        self.assertFalse(thread.is_alive())
        self.assertTrue(self.app._monitor_stop[self.meeting_id].is_set())


if __name__ == "__main__":
    unittest.main()
