from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Mapping

from ai_meeting_room.agents.adapter import AgentAdapter
from ai_meeting_room.core.safety import DispatchBlockedError
from ai_meeting_room.core.service import MeetingCore
from ai_meeting_room.models import AgentHealth, AgentRecord, AgentRole, AgentStatus, MeetingStatus, TaskStatus
from ai_meeting_room.persistence.sqlite_store import SQLiteStore
from ai_meeting_room.product.app import Phase2Application


class GateAdapter(AgentAdapter):
    def __init__(self, agent_id: str = "worker") -> None:
        self.agent_id = agent_id
        self.healthy = True
        self.status = "IDLE"
        self.output = ""
        self.sent: list[dict[str, Any]] = []
        self.interrupted = 0

    def start(self) -> str: return self.agent_id
    def stop(self) -> None: pass
    def pause(self) -> None: self.interrupt()
    def resume(self) -> None: pass
    def interrupt(self) -> None: self.interrupted += 1
    def send_task(self, task: Mapping[str, Any]) -> str:
        self.sent.append(dict(task))
        self.status = "WORKING"
        return str(task["task_id"])
    def get_status(self) -> str: return self.status
    def get_health(self) -> str: return AgentHealth.HEALTHY.value if self.healthy else AgentHealth.UNKNOWN.value
    def get_output(self) -> str: return self.output
    def health_check(self) -> bool: return self.healthy


class BlockingOutputAdapter(GateAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.raw_status = "working"
        self.block_next_output = False
        self.raise_after_release = False
        self.output_requested = threading.Event()
        self.release_output = threading.Event()

    def send_task(self, task: Mapping[str, Any]) -> str:
        result = super().send_task(task)
        self.block_next_output = True
        return result

    def get_output(self) -> str:
        if self.block_next_output:
            self.output_requested.set()
            if not self.release_output.wait(3):
                raise TimeoutError("test output release timed out")
            self.block_next_output = False
            if self.raise_after_release:
                raise RuntimeError("injected output failure")
        return self.output


class TaskAdvancementGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.temp_dir.name) / "state.db")
        self.core = MeetingCore.create("T4 gate", self.store, meeting_id="t4-gate")
        self.adapter = GateAdapter()
        self.core.add_agent(
            AgentRecord(
                "worker", "Worker", "test", AgentRole.WORKER,
                status=AgentStatus.IDLE, health=AgentHealth.HEALTHY,
            ),
            self.adapter,
        )
        self.core.mark_ready()
        self.core.start()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _pause(self) -> None:
        self.core.safety.observe_agent_failure("worker", "injected failure", AgentHealth.UNKNOWN)

    def _working_task(self, task_id: str = "working-task"):
        task = self.core.create_task("work", "read only", task_id=task_id)
        self.core.tasks.assign_task(task.task_id, "worker")
        self.core.tasks.dispatch_task(task.task_id)
        return task

    def test_paused_meeting_rejects_task_creation_without_persisting_or_publishing(self) -> None:
        self._pause()
        events_before = len(self.core.events.history("t4-gate"))

        with self.assertRaises(DispatchBlockedError):
            self.core.create_task("must not appear", "read only", task_id="paused-create")

        self.assertEqual(self.core.tasks.all(), ())
        self.assertEqual(self.store.list_tasks("t4-gate"), [])
        self.assertEqual(len(self.core.events.history("t4-gate")), events_before)
        self.assertEqual(self.core.meeting.meeting.status, MeetingStatus.PAUSED)

    def test_paused_meeting_rejects_assignment_without_reassigning_agent(self) -> None:
        task = self.core.create_task("pending", "read only", task_id="paused-assign")
        self._pause()

        with self.assertRaises(DispatchBlockedError):
            self.core.tasks.assign_task(task.task_id, "worker")

        self.assertEqual(self.core.tasks.get(task.task_id).status, TaskStatus.PENDING)
        self.assertIsNone(self.core.registry.get("worker").current_task_id)
        self.assertEqual(self.core.tasks.get(task.task_id).assigned_agent_id, None)

    def test_open_breaker_rejects_completion_even_if_meeting_record_is_running(self) -> None:
        task = self._working_task("open-complete")
        self.core.safety.breaker.open("external-trigger", "open without meeting transition")

        with self.assertRaises(DispatchBlockedError):
            self.core.tasks.complete_task(task.task_id, "unsafe result")

        self.assertEqual(self.core.meeting.meeting.status, MeetingStatus.RUNNING)
        self.assertEqual(self.core.tasks.get(task.task_id).status, TaskStatus.WORKING)
        self.assertIsNone(self.core.tasks.get(task.task_id).result)

    def test_paused_meeting_rejects_acceptance_without_publishing_task_accepted(self) -> None:
        task = self._working_task("paused-accept")
        self.core.tasks.complete_task(task.task_id, "verified output")
        self._pause()
        events_before = len(self.core.events.history("t4-gate"))

        with self.assertRaises(DispatchBlockedError):
            self.core.tasks.accept_task(task.task_id)

        self.assertFalse(self.core.tasks.get(task.task_id).accepted)
        self.assertEqual(len(self.core.events.history("t4-gate")), events_before)

    def test_paused_meeting_rejects_failure_and_cancellation_transitions(self) -> None:
        working = self._working_task("paused-fail")
        pending = self.core.create_task("pending", "read only", task_id="paused-cancel")
        self._pause()

        with self.assertRaises(DispatchBlockedError):
            self.core.tasks.fail_task(working.task_id, "late failure")
        with self.assertRaises(DispatchBlockedError):
            self.core.tasks.cancel_task(pending.task_id)

        self.assertEqual(self.core.tasks.get(working.task_id).status, TaskStatus.WORKING)
        self.assertEqual(self.core.tasks.get(pending.task_id).status, TaskStatus.PENDING)

    def test_blocked_dispatch_records_refusal_without_sending_to_runtime(self) -> None:
        task = self.core.create_task("blocked dispatch", "read only", task_id="blocked-dispatch")
        self.core.tasks.assign_task(task.task_id, "worker")
        self._pause()

        with self.assertRaises(DispatchBlockedError):
            self.core.tasks.dispatch_task(task.task_id)

        self.assertEqual(self.core.tasks.get(task.task_id).status, TaskStatus.BLOCKED)
        self.assertEqual(self.adapter.sent, [])

    def test_blocked_dispatch_can_be_explicitly_retried_after_health_gated_recovery(self) -> None:
        task = self.core.create_task("retry after pause", "read only", task_id="blocked-retry")
        self.core.tasks.assign_task(task.task_id, "worker")
        self._pause()
        with self.assertRaises(DispatchBlockedError):
            self.core.tasks.dispatch_task(task.task_id)
        self.assertEqual(self.core.tasks.get(task.task_id).status, TaskStatus.BLOCKED)
        self.assertEqual(self.adapter.sent, [])

        self.core.registry.update("worker", health=AgentHealth.HEALTHY, status=AgentStatus.IDLE)
        self.assertTrue(self.core.recover(cao_health_check=lambda: True, workspace_health_check=lambda: True))
        retried = self.core.tasks.dispatch_task(task.task_id)
        self.assertEqual(retried.status, TaskStatus.WORKING)
        self.assertEqual(len(self.adapter.sent), 1)

    def test_recovering_meeting_rejects_retry_assignment(self) -> None:
        task = self.core.create_task("retry", "read only", task_id="recovering-retry")
        self._pause()
        self.core.safety.breaker.begin_recovery()
        self.core.meeting.transition(MeetingStatus.RECOVERING)

        with self.assertRaises(DispatchBlockedError):
            self.core.tasks.assign_task(task.task_id, "worker")

        self.assertEqual(self.core.tasks.get(task.task_id).status, TaskStatus.PENDING)

    def test_unknown_cached_participant_trips_pause_before_task_creation(self) -> None:
        self.core.registry.update("worker", health=AgentHealth.UNKNOWN)

        with self.assertRaises(DispatchBlockedError):
            self.core.create_task("must pause", "read only", task_id="unknown-participant")

        self.assertEqual(self.core.meeting.meeting.status, MeetingStatus.PAUSED)
        self.assertEqual(self.core.safety.breaker.state.value, "OPEN")
        self.assertEqual(self.core.tasks.all(), ())

    def test_live_runtime_health_failure_trips_pause_before_assignment(self) -> None:
        task = self.core.create_task("runtime fault", "read only", task_id="runtime-fault")
        self.adapter.healthy = False

        with self.assertRaises(DispatchBlockedError):
            self.core.tasks.assign_task(task.task_id, "worker")

        self.assertEqual(self.core.meeting.meeting.status, MeetingStatus.PAUSED)
        self.assertEqual(self.core.safety.breaker.state.value, "OPEN")
        self.assertEqual(self.core.tasks.get(task.task_id).status, TaskStatus.PENDING)

    def test_inactive_runtime_binding_trips_pause_before_task_creation(self) -> None:
        self.core.registry.update("worker", runtime_state="RETIRED")

        with self.assertRaises(DispatchBlockedError):
            self.core.create_task("stale runtime", "read only", task_id="stale-runtime")

        self.assertEqual(self.core.meeting.meeting.status, MeetingStatus.PAUSED)
        self.assertEqual(self.core.safety.breaker.state.value, "OPEN")
        self.assertEqual(self.core.tasks.all(), ())

    def test_healthy_runtime_allows_normal_task_lifecycle(self) -> None:
        task = self._working_task("healthy-flow")
        completed = self.core.tasks.complete_task(task.task_id, "files: a.txt")
        accepted = self.core.tasks.accept_task(task.task_id)

        self.assertEqual(completed.status, TaskStatus.COMPLETED)
        self.assertTrue(accepted.accepted)
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(self.core.meeting.meeting.status, MeetingStatus.RUNNING)

    def test_task_watcher_does_not_complete_after_meeting_is_paused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = BlockingOutputAdapter()
            app = Phase2Application(
                SQLiteStore(Path(directory) / "product.db"),
                adapter_factory=lambda **_: adapter,
            )
            worker_errors: list[BaseException] = []
            previous_excepthook = threading.excepthook
            threading.excepthook = lambda args: worker_errors.append(args.exc_value)
            try:
                meeting_id = app.create_meeting("watcher gate", directory)["meeting"]["meeting_id"]
                agent_id = app.join_provider(meeting_id, "codex")["agents"][0]["agent_id"]
                app.start_meeting(meeting_id)
                task_id = app.create_task(meeting_id, "watch", "read only", agent_id)["tasks"][-1]["task_id"]
                app.dispatch_task(meeting_id, task_id)
                self.assertTrue(adapter.output_requested.wait(2))

                app.pause(meeting_id, "test pause before completion")
                adapter.raw_status = "completed"
                adapter.output = "finished result"
                adapter.release_output.set()

                watcher = app._task_threads[task_id]
                watcher.join(timeout=2)
                state = app.snapshot(meeting_id)
                task = next(item for item in state["tasks"] if item["task_id"] == task_id)
                event_types = [event.event_type for event in app._core(meeting_id).events.history(meeting_id)]

                self.assertFalse(watcher.is_alive())
                self.assertEqual(task["status"], TaskStatus.WORKING.value)
                self.assertNotIn("TaskCompleted", event_types)
                self.assertEqual(worker_errors, [])
            finally:
                adapter.release_output.set()
                app.close()
                threading.excepthook = previous_excepthook

    def test_task_watcher_fault_does_not_write_task_failure_after_global_pause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = BlockingOutputAdapter()
            app = Phase2Application(
                SQLiteStore(Path(directory) / "product.db"),
                adapter_factory=lambda **_: adapter,
            )
            worker_errors: list[BaseException] = []
            previous_excepthook = threading.excepthook
            threading.excepthook = lambda args: worker_errors.append(args.exc_value)
            try:
                meeting_id = app.create_meeting("watcher error gate", directory)["meeting"]["meeting_id"]
                agent_id = app.join_provider(meeting_id, "codex")["agents"][0]["agent_id"]
                app.start_meeting(meeting_id)
                task_id = app.create_task(meeting_id, "watch", "read only", agent_id)["tasks"][-1]["task_id"]
                app.dispatch_task(meeting_id, task_id)
                self.assertTrue(adapter.output_requested.wait(2))
                adapter.raise_after_release = True
                adapter.release_output.set()

                watcher = app._task_threads[task_id]
                watcher.join(timeout=2)
                state = app.snapshot(meeting_id)
                task = next(item for item in state["tasks"] if item["task_id"] == task_id)
                event_types = [event.event_type for event in app._core(meeting_id).events.history(meeting_id)]

                self.assertFalse(watcher.is_alive())
                self.assertEqual(state["meeting"]["status"], MeetingStatus.PAUSED.value)
                self.assertEqual(task["status"], TaskStatus.WORKING.value)
                self.assertNotIn("TaskFailed", event_types)
                self.assertEqual(worker_errors, [])
            finally:
                adapter.release_output.set()
                app.close()
                threading.excepthook = previous_excepthook

    def test_product_accept_and_rework_reject_before_persisting_brain_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = GateAdapter()
            app = Phase2Application(
                SQLiteStore(Path(directory) / "product.db"),
                adapter_factory=lambda **_: adapter,
            )
            try:
                meeting_id = app.create_meeting("decision gate", directory)["meeting"]["meeting_id"]
                agent_id = app.join_provider(meeting_id, "codex")["agents"][0]["agent_id"]
                app.start_meeting(meeting_id)
                task_id = app.create_task(meeting_id, "completed", "read only", agent_id)["tasks"][-1]["task_id"]
                core = app._core(meeting_id)
                core.tasks.dispatch_task(task_id)
                core.tasks.complete_task(task_id, "verified")
                app.pause(meeting_id, "decision gate pause")
                decisions_before = app.snapshot(meeting_id)["brainDecisions"]

                with self.assertRaises(DispatchBlockedError):
                    app.submit_brain_decision(meeting_id, {
                        "type": "ACCEPT", "relatedTaskId": task_id, "reason": "must be rejected",
                    })
                with self.assertRaises(DispatchBlockedError):
                    app.submit_brain_decision(meeting_id, {
                        "type": "REWORK", "targetAgentId": agent_id,
                        "relatedTaskId": task_id, "instruction": "must be rejected",
                    })

                self.assertEqual(app.snapshot(meeting_id)["brainDecisions"], decisions_before)
                self.assertFalse(core.tasks.get(task_id).accepted)
                self.assertEqual(len(core.tasks.all()), 1)
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
