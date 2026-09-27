from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from ai_meeting_room.agents.adapter import AgentAdapter
from ai_meeting_room.core.errors import MeetingLifecycleConflict
from ai_meeting_room.models import AgentHealth
from ai_meeting_room.persistence.sqlite_store import SQLiteStore
from ai_meeting_room.product.app import Phase2Application


class ContractTestAdapter(AgentAdapter):
    def __init__(self) -> None:
        self.agent_id = "phase3b-contract-terminal"
        self.raw_status = "idle"
        self.output = ""

    def start(self) -> str:
        return self.agent_id

    def stop(self) -> None:
        self.raw_status = "stopped"

    def pause(self) -> None:
        self.raw_status = "idle"

    def resume(self) -> None:
        raise RuntimeError("resume is explicit replacement-only")

    def interrupt(self) -> None:
        self.raw_status = "idle"

    def send_task(self, task):
        self.raw_status = "completed"
        self.output = "P3B_MANUAL_HANDOFF_LIVE_OK\nVALIDATION_NONCE=unit-test"
        return task["task_id"]

    @property
    def task_completion_observed(self) -> bool:
        return self.raw_status == "completed" and bool(self.output)

    def get_status(self) -> str:
        return "IDLE"

    def get_health(self) -> str:
        return AgentHealth.HEALTHY.value

    def get_output(self) -> str:
        return self.output

    def health_check(self) -> bool:
        return self.raw_status != "stopped"


class Phase3BIntegrationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "state.db"
        self.apps: list[Phase2Application] = []

    def tearDown(self) -> None:
        for app in reversed(self.apps):
            if not app._closed:
                app.close()
        self.temp.cleanup()

    def new_app(self) -> Phase2Application:
        app = Phase2Application(
            SQLiteStore(self.db_path),
            adapter_factory=lambda **_: ContractTestAdapter(),
            monitor_poll_interval_seconds=0.02,
        )
        self.apps.append(app)
        return app

    def create_joined_meeting(self, app: Phase2Application) -> tuple[str, str]:
        meeting_id = app.create_meeting("Phase 3B integration contract", self.temp.name)["meeting"]["meeting_id"]
        agent_id = app.join_provider(meeting_id, "codex")["agents"][0]["agent_id"]
        return meeting_id, agent_id

    def test_manual_gpt_brain_participant_persists(self) -> None:
        app = self.new_app()
        meeting_id, _ = self.create_joined_meeting(app)
        participant = app.snapshot(meeting_id)["brainParticipant"]

        persisted = app.store.get_brain_participant(meeting_id)

        self.assertEqual(persisted["participantId"], participant["participantId"])
        self.assertEqual(persisted["transport"], "MANUAL_GPT_HANDOFF")
        self.assertEqual(persisted["role"], "BRAIN")

    def test_manual_gpt_brain_id_stable_after_restart(self) -> None:
        app = self.new_app()
        meeting_id, _ = self.create_joined_meeting(app)
        before = app.snapshot(meeting_id)["brainParticipant"]
        app.close()

        restarted = self.new_app()
        after = restarted.snapshot(meeting_id)["brainParticipant"]

        self.assertEqual(after["participantId"], before["participantId"])

    def test_manual_gpt_transport_persists_after_restart(self) -> None:
        app = self.new_app()
        meeting_id, _ = self.create_joined_meeting(app)
        self.assertEqual(app.snapshot(meeting_id)["brainTransport"], "MANUAL_GPT_HANDOFF")
        app.close()

        restarted = self.new_app()
        self.assertEqual(restarted.snapshot(meeting_id)["brainTransport"], "MANUAL_GPT_HANDOFF")
        self.assertEqual(restarted.store.get_brain_participant(meeting_id)["transport"], "MANUAL_GPT_HANDOFF")

    def test_manual_gpt_brain_not_regenerated_on_snapshot(self) -> None:
        app = self.new_app()
        meeting_id, _ = self.create_joined_meeting(app)
        first = app.snapshot(meeting_id)["brainParticipant"]["participantId"]
        second = app.snapshot(meeting_id)["brainParticipant"]["participantId"]
        self.assertEqual(second, first)

    def test_created_meeting_does_not_dispatch_task(self) -> None:
        app = self.new_app()
        meeting_id, agent_id = self.create_joined_meeting(app)
        task_id = app.create_task(meeting_id, "not yet", "Read-only.", agent_id)["tasks"][-1]["task_id"]

        with self.assertRaises(MeetingLifecycleConflict):
            app.dispatch_task(meeting_id, task_id)

        task = next(item for item in app.snapshot(meeting_id)["tasks"] if item["task_id"] == task_id)
        self.assertEqual(task["status"], "QUEUED")

    def test_started_meeting_allows_task_dispatch(self) -> None:
        app = self.new_app()
        meeting_id, agent_id = self.create_joined_meeting(app)
        app.start_meeting(meeting_id)
        task_id = app.create_task(meeting_id, "started", "Read-only.", agent_id)["tasks"][-1]["task_id"]

        app.dispatch_task(meeting_id, task_id)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            task = next(item for item in app.snapshot(meeting_id)["tasks"] if item["task_id"] == task_id)
            if task["status"] == "COMPLETED":
                break
            time.sleep(0.02)

        task = next(item for item in app.snapshot(meeting_id)["tasks"] if item["task_id"] == task_id)
        self.assertEqual(task["status"], "COMPLETED")
        self.assertIn("P3B_MANUAL_HANDOFF_LIVE_OK", task["result"])

    def test_manual_gpt_meeting_uses_normal_start_transition(self) -> None:
        app = self.new_app()
        meeting_id, _ = self.create_joined_meeting(app)
        self.assertEqual(app.snapshot(meeting_id)["meeting"]["status"], "CREATED")

        started = app.start_meeting(meeting_id)

        self.assertEqual(started["meeting"]["status"], "RUNNING")
        self.assertEqual(started["brainTransport"], "MANUAL_GPT_HANDOFF")

    def test_real_agent_completion_commits_task_result_when_meeting_running(self) -> None:
        app = self.new_app()
        meeting_id, agent_id = self.create_joined_meeting(app)
        app.start_meeting(meeting_id)
        task_id = app.create_task(meeting_id, "commit result", "Read-only.", agent_id)["tasks"][-1]["task_id"]
        app.dispatch_task(meeting_id, task_id)

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            snapshot = app.snapshot(meeting_id)
            task = next(item for item in snapshot["tasks"] if item["task_id"] == task_id)
            if task["status"] == "COMPLETED":
                break
            time.sleep(0.02)

        task = next(item for item in app.snapshot(meeting_id)["tasks"] if item["task_id"] == task_id)
        self.assertEqual(task["status"], "COMPLETED")
        self.assertIsNotNone(task["result"])

    def test_agent_raw_output_does_not_bypass_task_engine(self) -> None:
        app = self.new_app()
        meeting_id, agent_id = self.create_joined_meeting(app)
        adapter = app._core(meeting_id).runtime.adapters[agent_id]
        adapter.output = "P3B_MANUAL_HANDOFF_LIVE_OK"

        task_id = app.create_task(meeting_id, "not dispatched", "Read-only.", agent_id)["tasks"][-1]["task_id"]
        task = next(item for item in app.snapshot(meeting_id)["tasks"] if item["task_id"] == task_id)

        self.assertEqual(task["status"], "QUEUED")
        self.assertIsNone(task["result"])

    def test_completed_result_can_create_manual_brain_request(self) -> None:
        app = self.new_app()
        meeting_id, agent_id = self.create_joined_meeting(app)
        app.start_meeting(meeting_id)
        task_id = app.create_task(meeting_id, "handoff", "Read-only.", agent_id)["tasks"][-1]["task_id"]
        app.dispatch_task(meeting_id, task_id)

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            handoff = app.snapshot(meeting_id).get("brainHandoff")
            if handoff:
                break
            time.sleep(0.02)

        snapshot = app.snapshot(meeting_id)
        self.assertEqual(next(item for item in snapshot["tasks"] if item["task_id"] == task_id)["status"], "COMPLETED")
        self.assertEqual(snapshot["brainHandoff"]["status"], "WAITING_FOR_HUMAN_HANDOFF")


if __name__ == "__main__":
    unittest.main()
