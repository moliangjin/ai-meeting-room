from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from ai_meeting_room.agents.adapter import AgentAdapter
from ai_meeting_room.core.safety import DispatchBlockedError
from ai_meeting_room.models import MeetingStatus
from ai_meeting_room.persistence.sqlite_store import SQLiteStore
from ai_meeting_room.product.app import Phase2Application, ProductError
from ai_meeting_room.product.server import UI_HTML, make_server


class CompletedTaskAdapter(AgentAdapter):
    def __init__(self) -> None:
        self.agent_id = "manual-gpt-worker"
        self.raw_status = "idle"
        self.output = ""
        self.sent = []
        self.interrupted = 0

    def start(self) -> str: return self.agent_id
    def stop(self) -> None: self.raw_status = "stopped"
    def pause(self) -> None: self.interrupt()
    def resume(self) -> None: self.raw_status = "idle"
    def interrupt(self) -> None: self.interrupted += 1
    def send_task(self, task): self.sent.append(dict(task)); self.raw_status = "completed"; self.output = "Read-only result: 2 files"; return task["task_id"]
    @property
    def task_completion_observed(self) -> bool: return self.raw_status == "completed" and bool(self.output)
    def get_status(self) -> str: return "IDLE"
    def get_health(self) -> str: return "HEALTHY"
    def get_output(self) -> str: return self.output
    def health_check(self) -> bool: return self.raw_status != "stopped"


class ManualGPTHandoffFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "state.db"
        self.apps = []
        self.app = self._new_app()
        self.meeting_id = self.app.create_meeting("Manual GPT flow", self.temp_dir.name)["meeting"]["meeting_id"]
        agent = self.app.join_provider(self.meeting_id, "codex")["agents"][0]
        self.agent_id = agent["agent_id"]
        self.app.start_meeting(self.meeting_id)
        task = self.app.create_task(self.meeting_id, "Count files", "List filenames only.", self.agent_id)["tasks"][-1]
        self.task_id = task["task_id"]
        self.app.dispatch_task(self.meeting_id, self.task_id)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            snap = self.app.snapshot(self.meeting_id)
            if next(t for t in snap["tasks"] if t["task_id"] == self.task_id)["status"] == "COMPLETED" and snap.get("brainHandoff"):
                break
            time.sleep(0.01)
        self.snapshot = self.app.snapshot(self.meeting_id)
        self.request = self.app.store.get_current_manual_gpt_handoff(self.meeting_id)

    def tearDown(self):
        for app in reversed(self.apps):
            if not app._closed:
                app.close()
        self.temp_dir.cleanup()

    def _new_app(self):
        created = []
        def factory(**_kwargs):
            adapter = CompletedTaskAdapter()
            created.append(adapter)
            return adapter
        app = Phase2Application(SQLiteStore(self.db_path), adapter_factory=factory, monitor_poll_interval_seconds=0.05)
        app._test_adapters = created
        self.apps.append(app)
        return app

    def _decision_payload(self, *, decision="ACCEPT", **overrides):
        packet = self.request["packet"]
        payload = {
            "schemaVersion": "ai-meeting-room.brain-decision-packet.v1",
            "decisionId": "decision-live-test-1",
            "requestId": packet["requestId"],
            "packetId": packet["packetId"],
            "meetingId": packet["meetingId"],
            "taskId": packet["taskId"],
            "resultId": packet["resultId"],
            "decision": decision,
            "reason": "Read-only evidence is acceptable.",
            "createdAt": "2026-09-21T04:00:00+00:00",
            "nonceEcho": packet["nonce"],
        }
        if decision == "REWORK":
            payload["reworkInstruction"] = "Repeat the read-only count and list filenames."
        payload.update(overrides)
        return json.dumps(payload)

    def _start_http_server(self):
        server = make_server(self.app, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _post_error(self, url, payload):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        return raised.exception.code, json.loads(raised.exception.read())["error"]

    def test_task_result_auto_creates_stable_waiting_packet_without_opening_breaker(self):
        self.assertEqual(self.snapshot["meeting"]["status"], "RUNNING")
        self.assertEqual(self.snapshot["circuit"]["circuitState"], "CLOSED")
        self.assertTrue(self.snapshot["brain"]["health"])
        self.assertEqual(self.snapshot["brain"]["state"], "WAITING_FOR_HUMAN_HANDOFF")
        self.assertEqual(self.request["status"], "WAITING_FOR_HUMAN_HANDOFF")
        self.assertEqual(self.request["packet"]["transport"], "MANUAL_GPT_HANDOFF")

    def test_copy_same_request_reuses_packet_request_nonce(self):
        first = self.app.create_manual_gpt_handoff(self.meeting_id, self.task_id)
        copied = self.app.copy_manual_gpt_handoff(self.meeting_id, first["requestId"])
        packet = self.app.store.get_manual_gpt_handoff_request(first["requestId"])["packet"]
        self.assertEqual(first["packetId"], copied["packetId"])
        self.assertEqual(first["requestId"], copied["requestId"])
        self.assertEqual(first["copyText"], copied["copyText"])
        self.assertTrue(copied["copyText"].startswith("【AI Meeting Room Brain Handoff｜Meeting → GPT】"))
        self.assertTrue(copied["copyText"].endswith("【Brain Handoff结束｜请GPT仅返回标准 BrainDecision Packet】"))
        self.assertIn(packet["nonce"], copied["copyText"])

    def test_accept_preview_then_core_apply_does_not_complete_meeting(self):
        copied = self.app.copy_manual_gpt_handoff(self.meeting_id, self.request["packet"]["requestId"])
        self.assertIn("【AI Meeting Room Brain Handoff｜Meeting → GPT】", copied["copyText"])
        validated = self.app.import_manual_gpt_decision(self.meeting_id, copied["requestId"], self._decision_payload())
        self.assertEqual(validated["status"], "VALIDATED")
        self.assertEqual(validated["preview"]["decision"], "ACCEPT")
        applied = self.app.apply_manual_gpt_decision(self.meeting_id, copied["requestId"])
        self.assertEqual(applied["meeting"]["status"], MeetingStatus.RUNNING.value)
        accepted = next(task for task in applied["tasks"] if task["task_id"] == self.task_id)
        self.assertTrue(accepted["accepted"])
        decision = applied["brainDecisions"][-1]
        self.assertEqual(decision["decisionId"], "decision-live-test-1")
        self.assertEqual(decision["transport"], "MANUAL_GPT_HANDOFF")
        self.assertTrue(decision["humanTransferred"])
        self.assertEqual(self.app.store.get_manual_gpt_handoff_request(copied["requestId"])["status"], "CONSUMED")

    def test_accept_clears_handoff_gate_for_the_next_task(self):
        request_id = self.request["packet"]["requestId"]
        self.app.copy_manual_gpt_handoff(self.meeting_id, request_id)
        self.app.import_manual_gpt_decision(self.meeting_id, request_id, self._decision_payload())
        self.app.apply_manual_gpt_decision(self.meeting_id, request_id)

        created = self.app.create_task(self.meeting_id, "Next task", "Read-only follow-up.", self.agent_id)

        self.assertEqual(len(created["tasks"]), 2)
        self.assertEqual(created["tasks"][-1]["title"], "Next task")
        self.assertEqual(created["meeting"]["status"], MeetingStatus.RUNNING.value)
        self.assertIsNone(created["brainHandoff"])

    def test_invalid_response_changes_no_meeting_or_task_state(self):
        request_id = self.request["packet"]["requestId"]
        self.app.copy_manual_gpt_handoff(self.meeting_id, request_id)
        before = self.app.snapshot(self.meeting_id)
        with self.assertRaises(ProductError) as raised:
            self.app.import_manual_gpt_decision(self.meeting_id, request_id, "not JSON")
        self.assertEqual(raised.exception.code, "INVALID_BRAIN_DECISION")
        after = self.app.snapshot(self.meeting_id)
        self.assertEqual(after["meeting"]["status"], before["meeting"]["status"])
        self.assertEqual(after["circuit"]["circuitState"], "CLOSED")
        self.assertFalse(next(t for t in after["tasks"] if t["task_id"] == self.task_id)["accepted"])

    def test_duplicate_import_is_rejected_and_never_applied_twice(self):
        request_id = self.request["packet"]["requestId"]
        self.app.copy_manual_gpt_handoff(self.meeting_id, request_id)
        payload = self._decision_payload()
        self.app.import_manual_gpt_decision(self.meeting_id, request_id, payload)
        with self.assertRaises(ProductError) as raised:
            self.app.import_manual_gpt_decision(self.meeting_id, request_id, payload)
        self.assertEqual(raised.exception.code, "DUPLICATE_DECISION_REJECTED")
        self.app.apply_manual_gpt_decision(self.meeting_id, request_id)
        with self.assertRaises(ProductError) as raised:
            self.app.apply_manual_gpt_decision(self.meeting_id, request_id)
        self.assertEqual(raised.exception.code, "DUPLICATE_DECISION_REJECTED")

    def test_consumed_request_rejects_second_decision(self):
        request_id = self.request["packet"]["requestId"]
        self.app.copy_manual_gpt_handoff(self.meeting_id, request_id)
        self.app.import_manual_gpt_decision(self.meeting_id, request_id, self._decision_payload())
        self.app.apply_manual_gpt_decision(self.meeting_id, request_id)
        with self.assertRaises(ProductError) as raised:
            self.app.import_manual_gpt_decision(
                self.meeting_id, request_id,
                self._decision_payload(decisionId="decision-after-consume"),
            )
        self.assertEqual(raised.exception.code, "DUPLICATE_DECISION_REJECTED")

    def test_rework_uses_existing_task_engine_and_binds_current_task(self):
        request_id = self.request["packet"]["requestId"]
        self.app.copy_manual_gpt_handoff(self.meeting_id, request_id)
        self.app.import_manual_gpt_decision(
            self.meeting_id, request_id,
            self._decision_payload(decision="REWORK", decisionId="decision-rework-1"),
        )
        applied = self.app.apply_manual_gpt_decision(self.meeting_id, request_id)
        child = next(task for task in applied["tasks"] if task.get("parent_task_id") == self.task_id)
        self.assertEqual(child["title"], "Rework")
        self.assertEqual(self.app._test_adapters[0].sent[-1]["task_id"], child["task_id"])
        self.assertEqual(applied["meeting"]["status"], MeetingStatus.RUNNING.value)

    def test_pause_uses_existing_safety_pause_path(self):
        request_id = self.request["packet"]["requestId"]
        self.app.copy_manual_gpt_handoff(self.meeting_id, request_id)
        self.app.import_manual_gpt_decision(
            self.meeting_id, request_id,
            self._decision_payload(decision="PAUSE", decisionId="decision-pause-1"),
        )
        applied = self.app.apply_manual_gpt_decision(self.meeting_id, request_id)
        self.assertEqual(applied["meeting"]["status"], "PAUSED")
        self.assertEqual(applied["circuit"]["circuitState"], "OPEN")

    def test_complete_meeting_is_distinct_from_accept(self):
        request_id = self.request["packet"]["requestId"]
        self.assertIn("COMPLETE_MEETING", self.request["packet"]["decisionTypesAllowed"])
        self.app.copy_manual_gpt_handoff(self.meeting_id, request_id)
        self.app.import_manual_gpt_decision(
            self.meeting_id, request_id,
            self._decision_payload(decision="COMPLETE_MEETING", decisionId="decision-complete-1"),
        )
        applied = self.app.apply_manual_gpt_decision(self.meeting_id, request_id)
        self.assertEqual(applied["meeting"]["status"], "COMPLETED")
        self.assertFalse(next(task for task in applied["tasks"] if task["task_id"] == self.task_id)["accepted"])

        server = make_server(self.app, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            create_request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/meetings/{self.meeting_id}/tasks",
                data=json.dumps({"title": "Too late", "instruction": "Read only"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(create_request, timeout=3)
            self.assertEqual(raised.exception.code, 409)
            body = json.loads(raised.exception.read())
            self.assertEqual(body["error"]["code"], "MEETING_TERMINAL")
            self.assertEqual(body["error"]["message"], "会议已结束，不能再创建任务。")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_common_user_errors_keep_stable_codes_and_chinese_messages(self):
        server, thread = self._start_http_server()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            status, error = self._post_error(
                f"{base}/api/meetings",
                {"name": "  ", "workspacePath": self.temp_dir.name},
            )
            self.assertEqual(status, 409)
            self.assertEqual(error["code"], "MEETING_NAME_REQUIRED")
            self.assertEqual(error["message"], "请填写会议名称。")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_consumed_brain_request_http_error_is_chinese_and_not_reapplied(self):
        request_id = self.request["packet"]["requestId"]
        payload = self._decision_payload()
        self.app.copy_manual_gpt_handoff(self.meeting_id, request_id)
        self.app.import_manual_gpt_decision(self.meeting_id, request_id, payload)
        self.app.apply_manual_gpt_decision(self.meeting_id, request_id)

        server, thread = self._start_http_server()
        try:
            status, error = self._post_error(
                f"http://127.0.0.1:{server.server_port}/api/meetings/{self.meeting_id}/brain-handoff/import",
                {"requestId": request_id, "rawResponse": payload},
            )
            self.assertEqual(status, 409)
            self.assertEqual(error["code"], "DUPLICATE_DECISION_REJECTED")
            self.assertEqual(error["message"], "该 Brain 决策已处理，不能重复导入或应用。")
            self.assertEqual(self.app.store.get_manual_gpt_handoff_request(request_id)["status"], "CONSUMED")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_lifecycle_conflicts_are_localized_over_product_http(self):
        request_id = self.request["packet"]["requestId"]
        self.app.copy_manual_gpt_handoff(self.meeting_id, request_id)
        self.app.import_manual_gpt_decision(
            self.meeting_id, request_id,
            self._decision_payload(decision="COMPLETE_MEETING", decisionId="decision-complete-http"),
        )
        self.app.apply_manual_gpt_decision(self.meeting_id, request_id)

        server, thread = self._start_http_server()
        base = f"http://127.0.0.1:{server.server_port}/api/meetings/{self.meeting_id}"
        try:
            status, error = self._post_error(f"{base}/start", {})
            self.assertEqual(status, 409)
            self.assertEqual(error["code"], "MEETING_NOT_STARTABLE")
            self.assertEqual(error["message"], "会议当前状态不允许开始。")

            status, error = self._post_error(f"{base}/recover", {})
            self.assertEqual(status, 409)
            self.assertEqual(error["code"], "MEETING_NOT_PAUSED")
            self.assertEqual(error["message"], "会议当前未处于暂停状态，不能恢复。")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_restart_restores_same_waiting_request_and_keeps_advancement_blocked(self):
        request_id = self.request["packet"]["requestId"]
        packet_id = self.request["packet"]["packetId"]
        nonce = self.request["packet"]["nonce"]
        self.app.copy_manual_gpt_handoff(self.meeting_id, request_id)
        self.app.close()
        restored_app = self._new_app()
        restored = restored_app.snapshot(self.meeting_id)
        handoff = restored_app.store.get_current_manual_gpt_handoff(self.meeting_id)
        self.assertEqual(handoff["packet"]["requestId"], request_id)
        self.assertEqual(handoff["packet"]["packetId"], packet_id)
        self.assertEqual(handoff["packet"]["nonce"], nonce)
        self.assertEqual(restored["brainHandoff"]["requestId"], request_id)
        self.assertEqual(restored["brain"]["state"], "WAITING_FOR_HUMAN_HANDOFF")
        with self.assertRaises(DispatchBlockedError):
            restored_app.create_task(self.meeting_id, "must not be created", "read only", self.agent_id)
        self.assertNotEqual(restored["meeting"]["status"], "RUNNING")
        self.assertEqual(len(restored["tasks"]), 1)
        repeated = restored_app.create_manual_gpt_handoff(self.meeting_id, self.task_id)
        self.assertEqual(repeated["requestId"], request_id)
        self.assertEqual(repeated["packetId"], packet_id)
        self.assertEqual(repeated["copyText"], handoff["copyText"])

    def test_manual_handoff_http_routes_copy_validate_and_apply(self):
        server = make_server(self.app, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}/api/meetings/{self.meeting_id}/brain-handoff"

        def post(operation, payload):
            request = urllib.request.Request(
                f"{base}/{operation}", data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                return json.loads(response.read())

        try:
            request_id = self.request["packet"]["requestId"]
            copied = post("copy", {"requestId": request_id})
            self.assertEqual(copied["requestId"], request_id)
            validated = post("import", {"requestId": request_id, "rawResponse": self._decision_payload()})
            self.assertEqual(validated["status"], "VALIDATED")
            applied = post("apply", {"requestId": request_id})
            self.assertEqual(applied["meeting"]["status"], MeetingStatus.RUNNING.value)
            self.assertTrue(next(t for t in applied["tasks"] if t["task_id"] == self.task_id)["accepted"])

            create_request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/meetings/{self.meeting_id}/tasks",
                data=json.dumps({"title": "HTTP follow-up", "instruction": "Read-only follow-up.", "agentId": self.agent_id}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(create_request, timeout=3) as response:
                created = json.loads(response.read())
            self.assertEqual(created["tasks"][-1]["title"], "HTTP follow-up")
            self.assertIsNone(created["brainHandoff"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_product_ui_has_manual_copy_import_validate_apply_without_clipboard_read(self):
        for marker in (
            "copyManualGPTPacket()", "brainDecisionImport", "validateManualGPTDecision()",
            "applyManualGPTDecision()", "WAITING_FOR_HUMAN_HANDOFF",
            "INVALID_BRAIN_DECISION:", "STALE_DECISION_REJECTED:",
            "DUPLICATE_DECISION_REJECTED:", "ACCEPT:'接受任务'", "REWORK:'返工'",
            "警告：应用后将结束整个会议。", "COMPLETE_MEETING",
        ):
            self.assertIn(marker, UI_HTML)
        self.assertNotIn('onclick="decision()"', UI_HTML)
        self.assertIn("/brain-handoff/apply", UI_HTML)
        self.assertNotIn("task.accept(", UI_HTML)
        self.assertNotIn("meeting.complete(", UI_HTML)
        self.assertNotIn("navigator.clipboard.readText", UI_HTML)
        self.assertNotIn("clipboard.readText", UI_HTML)
        snapshot = self.app.snapshot(self.meeting_id)
        self.assertEqual(snapshot["brainTransport"], "MANUAL_GPT_HANDOFF")
        self.assertFalse(snapshot["gptWebAutomation"]["default"])
        self.assertEqual(snapshot["gptWebAutomation"]["status"], "EXPERIMENTAL_BLOCKED_EXTERNAL_CHALLENGE")


if __name__ == "__main__":
    unittest.main()
