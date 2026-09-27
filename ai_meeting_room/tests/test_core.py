from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_meeting_room.agents.adapter import AgentAdapter
from ai_meeting_room.core.service import MeetingCore
from ai_meeting_room.models import AgentHealth, AgentRecord, AgentRole, AgentStatus, MeetingStatus, TaskStatus
from ai_meeting_room.persistence.sqlite_store import SQLiteStore
from ai_meeting_room.workspace.manager import WorkspaceManager


class FakeAdapter(AgentAdapter):
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.sent: list[dict] = []
        self.interrupts = 0
        self.healthy = True

    def start(self) -> str: return self.agent_id
    def stop(self) -> None: pass
    def pause(self) -> None: self.interrupt()
    def resume(self) -> None: pass
    def interrupt(self) -> None: self.interrupts += 1
    def send_task(self, task): self.sent.append(dict(task)); return task["task_id"]
    def get_status(self) -> str: return "IDLE"
    def get_health(self) -> str: return AgentHealth.HEALTHY.value if self.healthy else AgentHealth.UNKNOWN.value
    def get_output(self) -> str: return "fake output"
    def health_check(self) -> bool: return self.healthy


class CoreTests(unittest.TestCase):
    def test_task_lifecycle_and_sqlite_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "state.db")
            core = MeetingCore.create("test meeting", store, meeting_id="meeting-1")
            adapter = FakeAdapter("agent-1")
            core.add_agent(AgentRecord("agent-1", "Worker", "fake", AgentRole.WORKER, status=AgentStatus.IDLE, health=AgentHealth.HEALTHY), adapter)
            core.mark_ready(); core.start()
            task = core.tasks.create_task("count", "read only")
            core.meeting.add_task(task.task_id); core._save_meeting()
            core.tasks.assign_task(task.task_id, "agent-1")
            self.assertEqual(core.tasks.dispatch_task(task.task_id).status, TaskStatus.WORKING)
            core.tasks.complete_task(task.task_id, "0 files")
            self.assertEqual(store.get_meeting("meeting-1")["status"], MeetingStatus.RUNNING.value)
            self.assertEqual(store.list_tasks("meeting-1")[0]["status"], TaskStatus.COMPLETED.value)
            restored = MeetingCore.restore("meeting-1", store)
            self.assertEqual(restored.meeting.meeting.status, MeetingStatus.PAUSED)
            self.assertGreaterEqual(len(store.list_events("meeting-1")), 4)

    def test_fault_pauses_and_task_dispatch_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "state.db")
            core = MeetingCore.create("safety", store, meeting_id="meeting-2")
            adapters = []
            for agent_id in ("codex", "minimax"):
                adapter = FakeAdapter(agent_id); adapters.append(adapter)
                initial_status = AgentStatus.WORKING if agent_id == "codex" else AgentStatus.IDLE
                core.add_agent(AgentRecord(agent_id, agent_id, agent_id, AgentRole.WORKER, status=initial_status, health=AgentHealth.HEALTHY), adapter)
            core.mark_ready(); core.start()
            task = core.tasks.create_task("blocked", "must not send")
            core.tasks.assign_task(task.task_id, "codex")
            core.registry.update("codex", status=AgentStatus.WORKING)
            core.safety.observe_agent_failure("minimax", "terminal disappeared")
            self.assertEqual(core.meeting.meeting.status, MeetingStatus.PAUSED)
            self.assertTrue(core.safety.breaker.stop_dispatch)
            self.assertEqual([a.interrupts for a in adapters], [1, 0])
            with self.assertRaises(Exception):
                core.tasks.dispatch_task(task.task_id)
            self.assertEqual(adapters[0].sent, [])
            self.assertEqual(core.tasks.get(task.task_id).status, TaskStatus.BLOCKED)

    def test_controlled_recovery_requires_health_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "state.db")
            core = MeetingCore.create("recovery", store, meeting_id="meeting-4")
            recovery_adapters = {}
            for agent_id in ("codex", "minimax"):
                adapter = FakeAdapter(agent_id)
                recovery_adapters[agent_id] = adapter
                core.add_agent(AgentRecord(agent_id, agent_id, agent_id, AgentRole.WORKER, status=AgentStatus.IDLE, health=AgentHealth.HEALTHY), adapter)
            core.mark_ready(); core.start()
            core.safety.observe_agent_failure("minimax", "terminal disappeared")
            self.assertEqual(core.meeting.meeting.status, MeetingStatus.PAUSED)
            recovery_adapters["minimax"].healthy = False
            self.assertFalse(core.recover(cao_health_check=lambda: True, workspace_health_check=lambda: True))
            self.assertEqual(core.meeting.meeting.status, MeetingStatus.PAUSED)
            self.assertEqual(core.safety.breaker.state.value, "OPEN")
            recovery_adapters["minimax"].healthy = True
            self.assertTrue(core.recover(cao_health_check=lambda: True, workspace_health_check=lambda: True))
            self.assertEqual(core.meeting.meeting.status, MeetingStatus.RUNNING)
            self.assertEqual(core.safety.breaker.state.value, "CLOSED")

    def test_recovery_late_failure_reopens_breaker_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "state.db")
            core = MeetingCore.create("late recovery failure", store, meeting_id="meeting-late-recovery")
            adapter = FakeAdapter("codex")
            core.add_agent(AgentRecord("codex", "Codex", "codex", AgentRole.WORKER, status=AgentStatus.IDLE, health=AgentHealth.HEALTHY), adapter)
            core.mark_ready(); core.start()
            core.safety.observe_agent_failure("codex", "terminal disappeared")

            # The first persist is begin_recovery; the second is the late
            # failure after the breaker has moved toward CLOSED.  The repair
            # path must reopen the circuit and leave the Meeting PAUSED.
            with patch.object(core.safety.breaker, "_persist", side_effect=[None, RuntimeError("late persistence failure")]):
                self.assertFalse(core.recover(cao_health_check=lambda: True, workspace_health_check=lambda: True))
            self.assertEqual(core.meeting.meeting.status, MeetingStatus.PAUSED)
            self.assertEqual(core.safety.breaker.state.value, "OPEN")
            self.assertTrue(core.safety.breaker.stop_dispatch)
            self.assertTrue(core.safety.breaker.workspace_write_protected)

    def test_restore_running_meeting_becomes_paused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "state.db")
            core = MeetingCore.create("restart", store, meeting_id="meeting-3")
            core.meeting.transition(MeetingStatus.READY); core.meeting.transition(MeetingStatus.RUNNING); core._save_meeting()
            restored = MeetingCore.restore("meeting-3", store)
            self.assertEqual(restored.meeting.meeting.status, MeetingStatus.PAUSED)

    def test_worktree_manager_returns_isolated_change_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"; repo.mkdir()
            import subprocess
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "README.md").write_text("base\n")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-q", "-m", "base"], cwd=repo, check=True)
            manager = WorkspaceManager(repo)
            path = manager.create_agent_worktree("codex", "task-1")
            (path / "worker.txt").write_text("isolated\n")
            self.assertIn("worker.txt", manager.get_status(path))
            self.assertTrue(manager.health_check())
            manager.remove_agent_worktree("codex", "task-1")


if __name__ == "__main__":
    unittest.main()
