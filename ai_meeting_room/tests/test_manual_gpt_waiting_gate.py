from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_meeting_room.core.service import MeetingCore
from ai_meeting_room.models import AgentHealth, AgentRecord, AgentRole, AgentStatus, MeetingStatus
from ai_meeting_room.persistence.sqlite_store import SQLiteStore
from ai_meeting_room.tasks.engine import HumanHandoffPendingError


class ManualGPTWaitingGateTests(unittest.TestCase):
    def test_waiting_handoff_blocks_advancement_without_becoming_brain_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            core = MeetingCore.create("manual wait", SQLiteStore(Path(directory) / "state.db"), meeting_id="manual-wait")
            adapter = GateTestAdapter()
            core.add_agent(AgentRecord("agent-1", "Agent", "test", AgentRole.WORKER,
                                       status=AgentStatus.IDLE, health=AgentHealth.HEALTHY), adapter=adapter)
            core.mark_ready()
            core.start()
            queued = core.create_task("already queued", "read only")
            core.tasks.assign_task(queued.task_id, "agent-1")
            pending = core.create_task("awaiting assignment", "read only")
            core.set_waiting_for_human_handoff(True)

            with self.assertRaises(HumanHandoffPendingError):
                core.create_task("must not be created", "read only")
            with self.assertRaises(HumanHandoffPendingError):
                core.tasks.assign_task(pending.task_id, "agent-1")
            with self.assertRaises(HumanHandoffPendingError):
                core.tasks.dispatch_task(queued.task_id)

            self.assertEqual(core.meeting.meeting.status, MeetingStatus.RUNNING)
            self.assertEqual(core.safety.breaker.state.value, "CLOSED")
            self.assertFalse(core.safety.breaker.stop_dispatch)
            self.assertEqual(core.tasks.get(queued.task_id).status.value, "QUEUED")
            self.assertIsNone(core.tasks.get(pending.task_id).assigned_agent_id)
            self.assertEqual(adapter.sent, [])
            self.assertEqual(len(core.tasks.all()), 2)


class GateTestAdapter:
    def __init__(self):
        self.agent_id = "agent-1"
        self.sent = []

    def start(self): return "agent-1"
    def stop(self): pass
    def pause(self): pass
    def resume(self): pass
    def send_task(self, task): self.sent.append(task)
    def interrupt(self): pass
    def get_status(self): return "IDLE"
    def get_health(self): return "HEALTHY"
    def get_output(self): return ""
    def health_check(self): return True


if __name__ == "__main__":
    unittest.main()
