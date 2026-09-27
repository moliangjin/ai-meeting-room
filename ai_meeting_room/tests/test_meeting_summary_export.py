from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from ai_meeting_room.agents.adapter import AgentAdapter
from ai_meeting_room.models import AgentHealth, AgentRecord, AgentRole, AgentStatus, MeetingRecord, MeetingStatus, TaskRecord, TaskStatus
from ai_meeting_room.persistence.sqlite_store import SQLiteStore
from ai_meeting_room.product.app import Phase2Application
from ai_meeting_room.product.meeting_summary import MeetingSummaryExporter
from ai_meeting_room.product.server import UI_HTML, make_server


class SummaryAdapter(AgentAdapter):
    def __init__(self) -> None:
        self.agent_id = "summary-agent"
        self.output = ""
        self.stopped = False

    def start(self) -> str:
        return self.agent_id

    def stop(self) -> None:
        self.stopped = True

    def pause(self) -> None:
        return None

    def resume(self) -> None:
        return None

    def interrupt(self) -> None:
        return None

    def send_task(self, task):
        self.output = "formal task output"
        return task["task_id"]

    def get_status(self) -> str:
        return "IDLE"

    def get_health(self) -> str:
        return AgentHealth.HEALTHY.value

    def get_output(self) -> str:
        return self.output

    def health_check(self) -> bool:
        return not self.stopped


class MeetingSummaryExportTests(unittest.TestCase):
    def _apply_manual_gpt_accept(self, app: Phase2Application, meeting_id: str, task_id: str) -> None:
        """Exercise the persisted Manual GPT decision path for the pilot state."""
        handoff = app.create_manual_gpt_handoff(meeting_id, task_id)
        copied = app.copy_manual_gpt_handoff(meeting_id, handoff["requestId"])
        packet = app.store.get_manual_gpt_handoff_request(copied["requestId"])["packet"]
        response = {
            "schemaVersion": "ai-meeting-room.brain-decision-packet.v1",
            "decisionId": "summary-restart-accept-1",
            "requestId": packet["requestId"],
            "packetId": packet["packetId"],
            "meetingId": packet["meetingId"],
            "taskId": packet["taskId"],
            "resultId": packet["resultId"],
            "decision": "ACCEPT",
            "reason": "The persisted read-only result is acceptable.",
            "createdAt": "2026-09-22T01:08:00+00:00",
            "nonceEcho": packet["nonce"],
        }
        validated = app.import_manual_gpt_decision(meeting_id, copied["requestId"], json.dumps(response))
        self.assertEqual(validated["status"], "VALIDATED")
        applied = app.apply_manual_gpt_decision(meeting_id, copied["requestId"])
        self.assertTrue(next(task for task in applied["tasks"] if task["task_id"] == task_id)["accepted"])

    def test_formatter_covers_required_content_and_excludes_secrets(self) -> None:
        meeting = MeetingRecord(
            meeting_id="meeting-summary-1",
            name="Release review",
            status=MeetingStatus.COMPLETED,
            created_at="2026-09-22T01:00:00+00:00",
            started_at="2026-09-22T01:01:00+00:00",
            completed_at="2026-09-22T01:10:00+00:00",
        )
        agent = AgentRecord(
            agent_id="agent-1",
            display_name="Codex",
            provider="codex",
            role=AgentRole.WORKER,
            status=AgentStatus.IDLE,
            health=AgentHealth.HEALTHY,
            runtime_state="ACTIVE",
        )
        task = TaskRecord(
            task_id="task-1",
            meeting_id=meeting.meeting_id,
            assigned_agent_id=agent.agent_id,
            title="Review the release",
            instruction="Read only",
            status=TaskStatus.COMPLETED,
            result="Reviewed 3 files. Notes are concise and read-only.",
            accepted=True,
        )
        events = [
            {"event_id": "1", "event_type": "MeetingPaused", "timestamp": "2026-09-22T01:04:00+00:00", "source": "SafetyEngine", "payload": json.dumps({"reason": "agent fault", "from": "RUNNING", "to": "PAUSED"})},
            {"event_id": "2", "event_type": "MeetingRecovering", "timestamp": "2026-09-22T01:06:00+00:00", "source": "RecoveryManager", "payload": json.dumps({"from": "PAUSED", "to": "RECOVERING"})},
            {"event_id": "3", "event_type": "MeetingResumed", "timestamp": "2026-09-22T01:07:00+00:00", "source": "MeetingCore", "payload": json.dumps({"from": "READY", "to": "RUNNING"})},
            {"event_id": "4", "event_type": "TaskAccepted", "timestamp": "2026-09-22T01:08:00+00:00", "source": "TaskEngine", "payload": json.dumps({"taskId": "task-1"})},
            {"event_id": "5", "event_type": "CircuitBreakerOpened", "timestamp": "2026-09-22T01:04:00+00:00", "source": "SafetyEngine", "payload": json.dumps({"triggerAgentId": "agent-1", "reason": "agent fault"})},
        ]
        decisions = [{
            "decisionId": "decision-1",
            "meetingId": meeting.meeting_id,
            "type": "REWORK",
            "relatedTaskId": "task-1",
            "reason": "Please verify the edge case",
            "instruction": "REWORK instruction: rerun the read-only check",
            "timestamp": "2026-09-22T01:05:00+00:00",
        }, {
            "decisionId": "decision-2",
            "meetingId": meeting.meeting_id,
            "type": "ACCEPT",
            "relatedTaskId": "task-1",
            "reason": "Looks good; password=do-not-export sk-proj-1234567890123456",
            "timestamp": "2026-09-22T01:08:00+00:00",
        }]

        document = MeetingSummaryExporter().build(
            meeting=meeting,
            agents=[agent],
            tasks=[task],
            safety={
                "circuitState": "CLOSED",
                "stopDispatch": False,
                "workspaceWriteProtected": False,
                "triggerAgentId": "agent-1",
                "triggerReason": "agent fault",
            },
            events=events,
            brain_events=[{"eventId": "brain-1", "eventType": "TaskCompleted", "timestamp": "2026-09-22T01:03:00+00:00"}],
            brain_decisions=decisions,
            brain_participant={"participantId": "brain-1", "provider": "MANUAL_GPT_HANDOFF", "health": "HEALTHY"},
            brain_transport="MANUAL_GPT_HANDOFF",
        )

        markdown = document.markdown
        for marker in (
            "Release review", "meeting-summary-1", "COMPLETED", "Created:",
            "Participants", "MANUAL_GPT_HANDOFF", "Review the release", "COMPLETED",
            "Reviewed 3 files", "GPT Brain Decisions", "REWORK", "REWORK instruction:",
            "ACCEPT Records", "Pause / Resume Events", "MeetingPaused", "MeetingResumed",
            "Safety", "CircuitBreakerOpened", "Audit Timeline Summary",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, markdown)
        self.assertNotIn("password=do-not-export", markdown)
        self.assertNotIn("sk-proj-1234567890123456", markdown)
        self.assertGreater(document.redacted_field_count, 0)

    def test_formatter_task_order_is_stable_across_runtime_and_persistence_order(self) -> None:
        meeting = MeetingRecord(
            meeting_id="meeting-order-1",
            name="Deterministic export",
            status=MeetingStatus.COMPLETED,
        )
        earlier = TaskRecord(
            task_id="task-z",
            meeting_id=meeting.meeting_id,
            assigned_agent_id=None,
            title="Earlier task",
            instruction="Read only",
            status=TaskStatus.COMPLETED,
            created_at="2026-09-22T01:00:00+00:00",
            result="Earlier result",
        )
        later = TaskRecord(
            task_id="task-a",
            meeting_id=meeting.meeting_id,
            assigned_agent_id=None,
            title="Later task",
            instruction="Read only",
            status=TaskStatus.COMPLETED,
            created_at="2026-09-22T02:00:00+00:00",
            result="Later result",
        )
        arguments = {
            "meeting": meeting,
            "agents": [],
            "safety": {},
            "events": [],
            "brain_events": [],
            "brain_decisions": [],
            "brain_participant": None,
            "brain_transport": "MANUAL_GPT_HANDOFF",
        }

        runtime_order = MeetingSummaryExporter().build(tasks=[earlier, later], **arguments)
        persistence_order = MeetingSummaryExporter().build(tasks=[later, earlier], **arguments)

        self.assertEqual(runtime_order.markdown, persistence_order.markdown)
        self.assertLess(runtime_order.markdown.index("Earlier task"), runtime_order.markdown.index("Later task"))

    def test_application_export_audits_metadata_only_and_uses_formal_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "state.db")
            app = Phase2Application(store, adapter_factory=lambda **_: SummaryAdapter())
            try:
                meeting_id = app.create_meeting("Formal export", directory)["meeting"]["meeting_id"]
                joined = app.join_provider(meeting_id, "codex")
                app.start_meeting(meeting_id)
                task_id = app.create_task(meeting_id, "Export task", "Read only", joined["agents"][0]["agent_id"])["tasks"][-1]["task_id"]
                core = app._core(meeting_id)
                core.tasks.dispatch_task(task_id)
                core.tasks.complete_task(task_id, "Agent result summary")
                core.tasks.accept_task(task_id)
                # Exercise persisted pause/resume and safety state before the
                # final summary is generated.
                app.pause(meeting_id, "operator pause")
                self.assertTrue(core.recover(cao_health_check=lambda: True, workspace_health_check=lambda: True, brain_health_check=lambda: True))
                app.complete_meeting(meeting_id, reason="final operator completion")

                result = app.export_meeting_summary(meeting_id, action="preview")
                self.assertEqual(result["format"], "markdown")
                self.assertIn("Agent result summary", result["markdown"])
                self.assertIn("COMPLETED", result["markdown"])
                rows = [row for row in store.list_events(meeting_id, limit=None) if row["event_type"] == "MEETING_SUMMARY_EXPORTED"]
                self.assertEqual(len(rows), 1)
                payload = json.loads(rows[0]["payload"])
                self.assertEqual(payload["format"], "markdown")
                self.assertEqual(payload["meetingId"], meeting_id)
                self.assertEqual(payload["action"], "preview")
                self.assertNotIn("markdown", payload)
                self.assertNotIn("Agent result summary", json.dumps(payload))
            finally:
                app.close()

    def test_export_is_stable_after_product_shell_restart_and_recovery(self) -> None:
        """Persisted Phase3D pilot state must render the same safe Markdown after reload."""
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            app = Phase2Application(
                SQLiteStore(database),
                adapter_factory=lambda **_: SummaryAdapter(),
                monitor_poll_interval_seconds=0.05,
            )
            meeting_id = app.create_meeting("Restart-safe export", directory)["meeting"]["meeting_id"]
            joined = app.join_provider(meeting_id, "codex")
            agent_id = joined["agents"][0]["agent_id"]
            app.start_meeting(meeting_id)
            task_id = app.create_task(meeting_id, "Persisted export task", "Read only", agent_id)["tasks"][-1]["task_id"]
            core = app._core(meeting_id)
            core.tasks.dispatch_task(task_id)
            core.tasks.complete_task(task_id, "Persisted Agent result")
            self._apply_manual_gpt_accept(app, meeting_id, task_id)

            # Exercise the formal pause -> health-gated recovery -> pause path,
            # leaving a durable OPEN safety checkpoint for the reload.
            app.pause(meeting_id, "checkpoint before Product Shell reload")
            self.assertTrue(core.recover(
                cao_health_check=lambda: True,
                workspace_health_check=lambda: True,
                brain_health_check=lambda: True,
            ))
            resumed = app.snapshot(meeting_id)
            self.assertEqual(resumed["meeting"]["status"], MeetingStatus.RUNNING.value)
            self.assertEqual(resumed["circuit"]["circuitState"], "CLOSED")
            app.pause(meeting_id, "final persisted safety checkpoint")
            before = app.snapshot(meeting_id)
            first = app.export_meeting_summary(meeting_id, action="preview")
            participant_id = before["brainParticipant"]["participantId"]
            self.assertEqual(before["meeting"]["status"], MeetingStatus.PAUSED.value)
            self.assertEqual(before["circuit"]["circuitState"], "OPEN")
            self.assertTrue(next(task for task in before["tasks"] if task["task_id"] == task_id)["accepted"])
            self.assertEqual(len(before["brainDecisions"]), 1)

            app.close()

            reloaded = Phase2Application(
                SQLiteStore(database),
                adapter_factory=lambda **_: SummaryAdapter(),
                monitor_poll_interval_seconds=0.05,
            )
            try:
                after = reloaded.snapshot(meeting_id)
                second = reloaded.export_meeting_summary(meeting_id, action="preview")
                self.assertEqual(second["markdown"], first["markdown"])
                self.assertEqual(after["meeting"]["status"], MeetingStatus.PAUSED.value)
                self.assertEqual(after["circuit"]["circuitState"], "OPEN")
                self.assertEqual(after["brainParticipant"]["participantId"], participant_id)
                restored_task = next(task for task in after["tasks"] if task["task_id"] == task_id)
                self.assertEqual(restored_task["result"], "Persisted Agent result")
                self.assertTrue(restored_task["accepted"])
                self.assertEqual(len(after["brainDecisions"]), 1)
                self.assertTrue(any(event["event_type"] == "MeetingRecovering" for event in after["events"]))

                export_rows = [
                    row for row in reloaded.store.list_events(meeting_id, limit=None)
                    if row["event_type"] == "MEETING_SUMMARY_EXPORTED"
                ]
                self.assertEqual(len(export_rows), 2)
                for row in export_rows:
                    payload = json.loads(row["payload"])
                    self.assertNotIn("markdown", payload)
                    self.assertNotIn("Persisted Agent result", json.dumps(payload))
                self.assertEqual(len(reloaded.store.list_tasks(meeting_id)), 1)
                self.assertEqual(len(reloaded.store.list_brain_decisions(meeting_id)), 1)
            finally:
                reloaded.close()

    def test_product_shell_ui_and_http_route_offer_preview_and_copy(self) -> None:
        self.assertIn("导出会议总结", UI_HTML)
        self.assertIn("/summary-export", UI_HTML)
        self.assertIn("复制 Markdown", UI_HTML)
        self.assertIn("navigator.clipboard?.writeText", UI_HTML)

        with tempfile.TemporaryDirectory() as directory:
            app = Phase2Application(SQLiteStore(Path(directory) / "state.db"), adapter_factory=lambda **_: SummaryAdapter())
            server = make_server(app, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                meeting_id = app.create_meeting("HTTP summary", directory)["meeting"]["meeting_id"]
                request = urllib.request.Request(
                    f"{base}/api/meetings/{meeting_id}/summary-export",
                    data=json.dumps({"action": "preview"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    body = json.loads(response.read().decode("utf-8"))
                self.assertTrue(body["ok"])
                self.assertIn("# Meeting Summary", body["markdown"])
                self.assertEqual(body["metadata"]["action"], "preview")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                app.close()


if __name__ == "__main__":
    unittest.main()
