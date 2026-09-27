from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from ai_meeting_room.agents.adapter import AgentAdapter
from ai_meeting_room.persistence.sqlite_store import SQLiteStore
from ai_meeting_room.product.app import Phase2Application, ProviderBlockedError
from ai_meeting_room.product.server import make_server
from ai_meeting_room.core.errors import MeetingLifecycleConflict
from ai_meeting_room.core.safety import DispatchBlockedError
from ai_meeting_room.models import AgentHealth, AgentStatus, MeetingStatus


class FastCodexAdapter(AgentAdapter):
    def __init__(self) -> None:
        self.agent_id = "cao-terminal-test"
        self.raw_status = "idle"
        self.output = ""
        self.interrupts = 0

    def start(self) -> str: return self.agent_id
    def stop(self) -> None: self.raw_status = "stopped"
    def pause(self) -> None: self.interrupt()
    def resume(self) -> None: raise RuntimeError("resume is explicit replacement-only")
    def interrupt(self) -> None: self.interrupts += 1; self.raw_status = "idle"
    def send_task(self, task): self.raw_status = "completed"; self.output = "2 files: a.txt, b.txt"; return task["task_id"]
    @property
    def task_completion_observed(self) -> bool: return self.raw_status == "completed" and bool(self.output)
    def get_status(self) -> str: return "IDLE"
    def get_health(self) -> str: return AgentHealth.HEALTHY.value
    def get_output(self) -> str: return self.output
    def health_check(self) -> bool: return self.raw_status != "stopped"


class QuotaCodexAdapter(FastCodexAdapter):
    """Deterministic adapter-side reproduction of a real quota banner."""

    def __init__(self) -> None:
        super().__init__()
        self.quota_exhausted = False

    def get_status(self) -> str:
        return "ERROR" if self.quota_exhausted else super().get_status()

    def get_output(self) -> str:
        if self.quota_exhausted:
            self.raw_status = "error"
            return "You've hit your usage limit."
        return super().get_output()

    def health_check(self) -> bool:
        return not self.quota_exhausted and super().health_check()


class Phase2ProductTests(unittest.TestCase):
    @contextmanager
    def app_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            apps: list[Phase2Application] = []

            def create_app(*, adapter_factory=None) -> Phase2Application:
                app = Phase2Application(
                    SQLiteStore(Path(directory) / "state.db"),
                    adapter_factory=adapter_factory or (lambda **_: FastCodexAdapter()),
                )
                apps.append(app)
                return app

            try:
                yield directory, create_app
            finally:
                for app in reversed(apps):
                    app.close()

    def test_codex_manual_brain_accepts_real_adapter_result(self) -> None:
        with self.app_workspace() as (directory, create_app):
            app = create_app()
            meeting = app.create_meeting("Codex product flow", directory)["meeting"]["meeting_id"]
            joined = app.join_provider(meeting, "codex")
            agent_id = joined["agents"][0]["agent_id"]
            app.start_meeting(meeting)
            state = app.create_task(meeting, "Count files", "List filenames only.", agent_id)
            task_id = state["tasks"][0]["task_id"]
            app.dispatch_task(meeting, task_id)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and app.snapshot(meeting)["tasks"][0]["status"] != "COMPLETED":
                time.sleep(0.02)
            result = app.snapshot(meeting)
            self.assertEqual(result["tasks"][0]["status"], "COMPLETED")
            self.assertTrue(result["brainInbox"])
            app.submit_brain_decision(meeting, {"type": "ACCEPT", "relatedTaskId": task_id, "reason": "result accepted"})
            accepted = app.snapshot(meeting)
            self.assertEqual(accepted["meeting"]["status"], MeetingStatus.RUNNING.value)
            self.assertTrue(next(task for task in accepted["tasks"] if task["task_id"] == task_id)["accepted"])
            self.assertTrue(any(item.get("resolved") for item in accepted["brainInbox"] if item.get("taskId") == task_id))
            next_task = app.create_task(meeting, "Continue after accept", "Read only.", agent_id)
            self.assertEqual(next(task for task in next_task["tasks"] if task["title"] == "Continue after accept")["status"], "QUEUED")

    def test_acceptance_agent_sessions_use_the_configured_r70_namespace(self) -> None:
        session_ids: list[str] = []

        def adapter_factory(**kwargs):
            session_ids.append(kwargs["session_id"])
            return FastCodexAdapter()

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "meeting.db"
            with patch.dict(os.environ, {
                "AI_MEETING_ROOM_ACCEPTANCE_MODE": "1",
                "AI_MEETING_ROOM_ACCEPTANCE_SESSION_PREFIX": "aimr-v1-r70-a50cb28352-",
                "AI_MEETING_ROOM_DATA_DIR": directory,
                "AI_MEETING_ROOM_DB": str(database),
            }):
                app = Phase2Application(SQLiteStore(database), adapter_factory=adapter_factory)
                try:
                    meeting_id = app.create_meeting("Acceptance namespace", directory)["meeting"]["meeting_id"]
                    app.join_provider(meeting_id, "codex")
                finally:
                    app.close()

        self.assertEqual(len(session_ids), 1)
        self.assertRegex(session_ids[0], r"^aimr-v1-r70-a50cb28352-codex-[0-9a-f]{8}$")

    def test_accept_does_not_complete_meeting(self) -> None:
        with self.app_workspace() as (directory, create_app):
            app = create_app()
            meeting = app.create_meeting("Accept semantics", directory)["meeting"]["meeting_id"]
            agent_id = app.join_provider(meeting, "codex")["agents"][0]["agent_id"]
            app.start_meeting(meeting)
            task_id = app.create_task(meeting, "accepted task", "Read only.", agent_id)["tasks"][-1]["task_id"]
            app.dispatch_task(meeting, task_id)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and next(t for t in app.snapshot(meeting)["tasks"] if t["task_id"] == task_id)["status"] != "COMPLETED":
                time.sleep(0.02)
            app.submit_brain_decision(meeting, {"type": "ACCEPT", "relatedTaskId": task_id, "reason": "accepted"})
            self.assertEqual(app.snapshot(meeting)["meeting"]["status"], MeetingStatus.RUNNING.value)

    def test_accept_completes_task_only(self) -> None:
        with self.app_workspace() as (directory, create_app):
            app = create_app()
            meeting = app.create_meeting("Accept task", directory)["meeting"]["meeting_id"]
            agent_id = app.join_provider(meeting, "codex")["agents"][0]["agent_id"]
            app.start_meeting(meeting)
            task_id = app.create_task(meeting, "accepted task", "Read only.", agent_id)["tasks"][-1]["task_id"]
            app.dispatch_task(meeting, task_id)
            time.sleep(0.05)
            app.submit_brain_decision(meeting, {"type": "ACCEPT", "relatedTaskId": task_id})
            task = next(t for t in app.snapshot(meeting)["tasks"] if t["task_id"] == task_id)
            self.assertEqual(task["status"], "COMPLETED")
            self.assertTrue(task["accepted"])

    def test_accept_resolves_brain_inbox_item(self) -> None:
        with self.app_workspace() as (directory, create_app):
            app = create_app()
            meeting = app.create_meeting("Resolve inbox", directory)["meeting"]["meeting_id"]
            agent_id = app.join_provider(meeting, "codex")["agents"][0]["agent_id"]
            app.start_meeting(meeting)
            task_id = app.create_task(meeting, "inbox task", "Read only.", agent_id)["tasks"][-1]["task_id"]
            app.dispatch_task(meeting, task_id)
            time.sleep(0.05)
            app.submit_brain_decision(meeting, {"type": "ACCEPT", "relatedTaskId": task_id})
            items = [item for item in app.snapshot(meeting)["brainInbox"] if item.get("taskId") == task_id]
            self.assertTrue(items)
            self.assertTrue(all(item["resolved"] for item in items))

    def test_accept_allows_next_task(self) -> None:
        with self.app_workspace() as (directory, create_app):
            app = create_app()
            meeting = app.create_meeting("Continue", directory)["meeting"]["meeting_id"]
            agent_id = app.join_provider(meeting, "codex")["agents"][0]["agent_id"]
            app.start_meeting(meeting)
            task_id = app.create_task(meeting, "first", "Read only.", agent_id)["tasks"][-1]["task_id"]
            app.dispatch_task(meeting, task_id)
            time.sleep(0.05)
            app.submit_brain_decision(meeting, {"type": "ACCEPT", "relatedTaskId": task_id})
            followup = app.create_task(meeting, "second", "Read only.", agent_id)
            self.assertEqual(next(t for t in followup["tasks"] if t["title"] == "second")["status"], "QUEUED")

    def test_complete_meeting_transitions_to_completed(self) -> None:
        with self.app_workspace() as (directory, create_app):
            app = create_app()
            meeting = app.create_meeting("Complete", directory)["meeting"]["meeting_id"]
            app.join_provider(meeting, "codex")
            app.start_meeting(meeting)
            app.submit_brain_decision(meeting, {"type": "COMPLETE_MEETING", "reason": "operator complete"})
            self.assertEqual(app.snapshot(meeting)["meeting"]["status"], MeetingStatus.COMPLETED.value)

    def test_complete_meeting_rejected_with_active_task(self) -> None:
        with self.app_workspace() as (directory, create_app):
            app = create_app()
            meeting = app.create_meeting("Reject complete", directory)["meeting"]["meeting_id"]
            agent_id = app.join_provider(meeting, "codex")["agents"][0]["agent_id"]
            app.start_meeting(meeting)
            app.create_task(meeting, "pending", "Read only.", agent_id)
            with self.assertRaises(RuntimeError):
                app.submit_brain_decision(meeting, {"type": "COMPLETE_MEETING", "reason": "too early"})
            self.assertEqual(app.snapshot(meeting)["meeting"]["status"], MeetingStatus.RUNNING.value)

    def test_completed_meeting_rejects_new_task(self) -> None:
        with self.app_workspace() as (directory, create_app):
            app = create_app()
            meeting = app.create_meeting("Terminal", directory)["meeting"]["meeting_id"]
            app.join_provider(meeting, "codex")
            app.start_meeting(meeting)
            app.submit_brain_decision(meeting, {"type": "COMPLETE_MEETING"})
            with self.assertRaises(RuntimeError):
                app.create_task(meeting, "forbidden", "Read only.", None)

    def test_completed_meeting_after_restart_rejects_start_and_dispatch_at_lifecycle_gate(self) -> None:
        with self.app_workspace() as (directory, create_app):
            app = create_app()
            meeting = app.create_meeting("Terminal restart", directory)["meeting"]["meeting_id"]
            agent_id = app.join_provider(meeting, "codex")["agents"][0]["agent_id"]
            app.start_meeting(meeting)
            task_id = app.create_task(meeting, "accepted task", "Read only.", agent_id)["tasks"][-1]["task_id"]
            app.dispatch_task(meeting, task_id)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                task = next(item for item in app.snapshot(meeting)["tasks"] if item["task_id"] == task_id)
                if task["status"] == "COMPLETED":
                    break
                time.sleep(0.02)
            app.submit_brain_decision(meeting, {"type": "ACCEPT", "relatedTaskId": task_id})
            app.complete_meeting(meeting)
            app.close()

            restored = create_app()
            with self.assertRaises(MeetingLifecycleConflict) as start_error:
                restored.start_meeting(meeting)
            self.assertEqual(start_error.exception.code, "MEETING_NOT_STARTABLE")

            with self.assertRaises(MeetingLifecycleConflict) as dispatch_error:
                restored.dispatch_task(meeting, task_id)
            self.assertEqual(dispatch_error.exception.code, "MEETING_NOT_RUNNING")
            snapshot = restored.snapshot(meeting)
            self.assertEqual(snapshot["meeting"]["status"], MeetingStatus.COMPLETED.value)
            self.assertEqual(len(snapshot["tasks"]), 1)

    def test_rework_does_not_complete_meeting(self) -> None:
        with self.app_workspace() as (directory, create_app):
            app = create_app()
            meeting = app.create_meeting("Rework", directory)["meeting"]["meeting_id"]
            agent_id = app.join_provider(meeting, "codex")["agents"][0]["agent_id"]
            app.start_meeting(meeting)
            task_id = app.create_task(meeting, "first", "Read only.", agent_id)["tasks"][-1]["task_id"]
            app.dispatch_task(meeting, task_id)
            time.sleep(0.05)
            app.submit_brain_decision(meeting, {"type": "REWORK", "targetAgentId": agent_id, "relatedTaskId": task_id, "instruction": "Read only again."})
            self.assertEqual(app.snapshot(meeting)["meeting"]["status"], MeetingStatus.RUNNING.value)

    def test_reject_does_not_complete_meeting(self) -> None:
        with self.app_workspace() as (directory, create_app):
            app = create_app()
            meeting = app.create_meeting("Reject", directory)["meeting"]["meeting_id"]
            agent_id = app.join_provider(meeting, "codex")["agents"][0]["agent_id"]
            app.start_meeting(meeting)
            task_id = app.create_task(meeting, "first", "Read only.", agent_id)["tasks"][-1]["task_id"]
            app.dispatch_task(meeting, task_id)
            time.sleep(0.05)
            app.submit_brain_decision(meeting, {"type": "REJECT", "relatedTaskId": task_id, "reason": "not accepted"})
            self.assertEqual(app.snapshot(meeting)["meeting"]["status"], MeetingStatus.RUNNING.value)

    def test_blocked_minimax_cannot_join(self) -> None:
        with self.app_workspace() as (directory, create_app):
            app = create_app()
            meeting = app.create_meeting("Provider gate", directory)["meeting"]["meeting_id"]
            with self.assertRaises(ProviderBlockedError):
                app.join_provider(meeting, "minimax")

    def test_product_shell_exposes_provider_state_and_brain_contract(self) -> None:
        with self.app_workspace() as (directory, create_app):
            app = create_app()
            self.assertEqual(next(p for p in app.catalog.all() if p.provider_id == "minimax").availability.value, "BLOCKED")
            self.assertIn("ManualBrainBridge", app.snapshot(app.create_meeting("API meeting", directory)["meeting"]["meeting_id"])["brain"]["name"])

    def test_restart_restores_running_meeting_as_paused(self) -> None:
        with self.app_workspace() as (directory, create_app):
            app = create_app()
            meeting_id = app.create_meeting("Restart safety", directory)["meeting"]["meeting_id"]
            app.join_provider(meeting_id, "codex")
            app.start_meeting(meeting_id)
            self.assertEqual(app.snapshot(meeting_id)["meeting"]["status"], MeetingStatus.RUNNING.value)

            restarted = create_app()
            restored = restarted.snapshot(meeting_id)
            self.assertEqual(restored["meeting"]["status"], MeetingStatus.PAUSED.value)
            self.assertTrue(restored["meeting"]["pause_reason"])

    def test_dispatch_is_rejected_and_task_blocked_after_global_pause(self) -> None:
        with self.app_workspace() as (directory, create_app):
            app = create_app()
            meeting_id = app.create_meeting("Dispatch gate", directory)["meeting"]["meeting_id"]
            joined = app.join_provider(meeting_id, "codex")
            agent_id = joined["agents"][0]["agent_id"]
            app.start_meeting(meeting_id)
            state = app.create_task(meeting_id, "Must be blocked", "Do not run.", agent_id)
            task_id = next(task["task_id"] for task in state["tasks"] if task["title"] == "Must be blocked")
            app.pause(meeting_id, "operator fault injection")
            with self.assertRaises(DispatchBlockedError):
                app.dispatch_task(meeting_id, task_id)
            task = next(task for task in app.snapshot(meeting_id)["tasks"] if task["task_id"] == task_id)
            self.assertEqual(task["status"], "BLOCKED")

    def test_quota_error_opens_global_pause_before_dispatch(self) -> None:
        with self.app_workspace() as (directory, create_app):
            adapters = []

            def factory(**_):
                adapter = QuotaCodexAdapter()
                adapters.append(adapter)
                return adapter

            app = create_app(adapter_factory=factory)
            meeting_id = app.create_meeting("Quota mapping", directory)["meeting"]["meeting_id"]
            agent_id = app.join_provider(meeting_id, "codex")["agents"][0]["agent_id"]
            app.start_meeting(meeting_id)
            task_id = app.create_task(meeting_id, "Quota task", "Read only.", agent_id)["tasks"][-1]["task_id"]
            adapters[0].quota_exhausted = True

            with self.assertRaises(DispatchBlockedError):
                app.dispatch_task(meeting_id, task_id)

            snapshot = app.snapshot(meeting_id)
            self.assertEqual(snapshot["meeting"]["status"], "PAUSED")
            self.assertEqual(snapshot["circuit"]["circuitState"], "OPEN")
            self.assertTrue(snapshot["circuit"]["stopDispatch"])
            self.assertEqual(snapshot["circuit"]["triggerReason"], "runtime health=ERROR")
            self.assertEqual(next(task for task in snapshot["tasks"] if task["task_id"] == task_id)["status"], "BLOCKED")

    def test_runtime_replacement_updates_all_bindings(self) -> None:
        with self.app_workspace() as (directory, create_app):
            app = create_app()
            meeting_id = app.create_meeting("Runtime replacement", directory)["meeting"]["meeting_id"]
            agent_id = app.join_provider(meeting_id, "codex")["agents"][0]["agent_id"]
            core = app._core(meeting_id)
            app.start_meeting(meeting_id)
            app.pause(meeting_id, "replacement test")
            app.restart_agent(meeting_id, agent_id)
            binding = core.runtime.current_binding(agent_id)
            agent = core.registry.get(agent_id)
            self.assertEqual(binding.generation, 2)
            self.assertEqual(agent.runtime_generation, 2)
            self.assertEqual(core.tasks._adapter_generations[agent_id], 2)
            self.assertEqual(core.heartbeat.get(agent_id).runtime_generation, 2)
            self.assertEqual(core.runtime.retired_bindings[agent_id][0].state, "RETIRED")

    def test_old_runtime_generation_cannot_override_new_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            from ai_meeting_room.core.service import MeetingCore
            from ai_meeting_room.models import AgentRecord, AgentRole, AgentStatus
            store = SQLiteStore(Path(directory) / "state.db")
            core = MeetingCore.create("Generation fence", store)
            old = FastCodexAdapter(); new = FastCodexAdapter()
            core.add_agent(AgentRecord("agent-1", "Worker", "fake", AgentRole.WORKER, AgentStatus.IDLE, AgentHealth.HEALTHY), old)
            core.replace_runtime("agent-1", new)
            core.beat_agent("agent-1", AgentHealth.ERROR, runtime_generation=1, terminal_id="old-terminal")
            self.assertEqual(core.registry.get("agent-1").health, AgentHealth.HEALTHY)
            self.assertTrue(any(e.event_type == "STALE_RUNTIME_EVENT_IGNORED" for e in core.events.history()))

    def test_stale_heartbeat_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            from ai_meeting_room.core.service import MeetingCore
            from ai_meeting_room.models import AgentRecord, AgentRole
            store = SQLiteStore(Path(directory) / "state.db")
            core = MeetingCore.create("Heartbeat fence", store)
            core.add_agent(AgentRecord("agent-1", "Worker", "fake", AgentRole.WORKER, AgentStatus.IDLE, AgentHealth.HEALTHY), FastCodexAdapter())
            core.replace_runtime("agent-1", FastCodexAdapter())
            current = core.heartbeat.get("agent-1").last_seen_at
            core.beat_agent("agent-1", AgentHealth.ERROR, runtime_generation=1)
            self.assertEqual(core.heartbeat.get("agent-1").last_seen_at, current)

    def test_old_watcher_is_retired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            from ai_meeting_room.core.service import MeetingCore
            from ai_meeting_room.models import AgentRecord, AgentRole
            store = SQLiteStore(Path(directory) / "state.db")
            core = MeetingCore.create("Watcher fence", store)
            old = FastCodexAdapter(); new = FastCodexAdapter()
            core.add_agent(AgentRecord("agent-1", "Worker", "fake", AgentRole.WORKER, AgentStatus.IDLE, AgentHealth.HEALTHY), old)
            generation = core.runtime.current_binding("agent-1").generation
            core.replace_runtime("agent-1", new)
            self.assertFalse(core.runtime.is_current("agent-1", generation, old))
            self.assertTrue(core.runtime.is_current("agent-1", generation + 1, new))

    def test_replacement_runtime_becomes_active_only_after_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            from ai_meeting_room.core.service import MeetingCore
            from ai_meeting_room.models import AgentRecord, AgentRole
            store = SQLiteStore(Path(directory) / "state.db")
            core = MeetingCore.create("Healthy replacement", store)
            core.add_agent(AgentRecord("agent-1", "Worker", "fake", AgentRole.WORKER, AgentStatus.IDLE, AgentHealth.HEALTHY), FastCodexAdapter())
            failed = FastCodexAdapter(); failed.raw_status = "stopped"
            with self.assertRaises(RuntimeError):
                core.replace_runtime("agent-1", failed)
            self.assertEqual(core.runtime.current_binding("agent-1").generation, 1)
            self.assertEqual(core.registry.get("agent-1").runtime_generation, 1)

    def test_recovery_uses_active_runtime_generation(self) -> None:
        with self.app_workspace() as (directory, create_app):
            app = create_app()
            meeting_id = app.create_meeting("Recovery generation", directory)["meeting"]["meeting_id"]
            agent_id = app.join_provider(meeting_id, "codex")["agents"][0]["agent_id"]
            app.start_meeting(meeting_id)
            app.pause(meeting_id, "generation recovery")
            app.restart_agent(meeting_id, agent_id)
            core = app._core(meeting_id)
            self.assertTrue(core.recover(cao_health_check=lambda: True, workspace_health_check=lambda: True))
            self.assertEqual(core.runtime.current_binding(agent_id).generation, 2)
            self.assertIs(core.runtime.adapters[agent_id], core.tasks._adapters[agent_id])

    def test_restart_reattach_advances_persisted_runtime_generation(self) -> None:
        with self.app_workspace() as (directory, create_app):
            app = create_app()
            meeting_id = app.create_meeting("Restart generation", directory)["meeting"]["meeting_id"]
            agent_id = app.join_provider(meeting_id, "codex")["agents"][0]["agent_id"]
            app.start_meeting(meeting_id)
            self.assertEqual(app._core(meeting_id).runtime.current_binding(agent_id).generation, 1)
            app.close()

            restarted = create_app()
            restored = restarted.snapshot(meeting_id)
            self.assertEqual(restored["meeting"]["status"], MeetingStatus.PAUSED.value)
            restarted.restart_agent(meeting_id, agent_id)
            self.assertEqual(restarted._core(meeting_id).runtime.current_binding(agent_id).generation, 2)
            restarted.close()


if __name__ == "__main__":
    unittest.main()
