from __future__ import annotations

import unittest
import json

from ai_meeting_room.brain.manual_gpt import ManualGPTBrainTransport, ManualGPTHandoffError


class ManualGPTTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = ManualGPTBrainTransport()

    def packet(self):
        return self.transport.create_packet(
            meeting_id="meeting-1",
            task_id="task-1",
            result_id="result-1",
            brain_participant_id="brain-participant-1",
            created_at="2026-09-21T00:00:00+00:00",
            decision_types_allowed=("ACCEPT", "REWORK"),
            task_summary="Count files",
            task_instruction="List filenames only.",
            agent_result="2 files: a.txt, b.txt",
            current_task_state="COMPLETED",
            current_meeting_state="RUNNING",
            validation_context={"taskAccepted": False},
            safety_context={"circuitState": "CLOSED", "stopDispatch": False},
        )

    def decision_payload(self, packet, **overrides):
        payload = {
            "schemaVersion": "ai-meeting-room.brain-decision-packet.v1",
            "decisionId": "decision-1",
            "requestId": packet.data["requestId"],
            "packetId": packet.data["packetId"],
            "meetingId": packet.data["meetingId"],
            "taskId": packet.data["taskId"],
            "resultId": packet.data["resultId"],
            "decision": "ACCEPT",
            "reason": "The result satisfies the task.",
            "createdAt": "2026-09-21T00:01:00+00:00",
            "nonceEcho": packet.data["nonce"],
        }
        payload.update(overrides)
        return payload

    def test_brain_packet_contains_required_identity(self):
        packet = self.packet()
        for field in (
            "schemaVersion", "packetId", "requestId", "meetingId", "taskId",
            "resultId", "brainParticipantId", "createdAt", "nonce",
            "decisionTypesAllowed", "taskSummary", "taskInstruction", "agentResult",
            "validationContext", "safetyContext", "currentTaskState", "currentMeetingState",
        ):
            with self.subTest(field=field):
                self.assertIn(field, packet.data)
                self.assertTrue(packet.data[field] is not None)

    def test_brain_packet_minimizes_context(self):
        packet = self.transport.create_packet(
            meeting_id="meeting-1",
            task_id="task-1",
            result_id="result-1",
            brain_participant_id="brain-participant-1",
            decision_types_allowed=("ACCEPT",),
            task_summary="Count files",
            task_instruction="List filenames only.",
            agent_result="2 files.",
            current_task_state="COMPLETED",
            current_meeting_state="RUNNING",
            validation_context={
                "taskStatus": "COMPLETED",
                "taskAccepted": False,
                "allEvents": ["irrelevant history"],
                "databaseDump": "unrelated data",
            },
            safety_context={
                "circuitState": "CLOSED",
                "stopDispatch": False,
                "environment": {"PRIVATE": "must not be included"},
                "workspaceDiff": "unrelated project data",
            },
        )
        self.assertEqual(packet.data["validationContext"], {
            "taskStatus": "COMPLETED", "taskAccepted": False,
        })
        self.assertEqual(packet.data["safetyContext"], {
            "circuitState": "CLOSED", "stopDispatch": False,
        })
        self.assertNotIn("irrelevant history", packet.copy_text)
        self.assertNotIn("unrelated project data", packet.copy_text)

    def test_brain_packet_blocks_sensitive_content(self):
        with self.assertRaises(ManualGPTHandoffError) as caught:
            self.transport.create_packet(
                meeting_id="meeting-1",
                task_id="task-1",
                result_id="result-1",
                brain_participant_id="brain-participant-1",
                decision_types_allowed=("ACCEPT",),
                task_summary="Count files",
                task_instruction="Read config; OPENAI_API_KEY=sk-proj-12345678901234567890",
                agent_result="2 files.",
                current_task_state="COMPLETED",
                current_meeting_state="RUNNING",
                validation_context={"taskStatus": "COMPLETED", "taskAccepted": False},
                safety_context={"circuitState": "CLOSED", "stopDispatch": False},
            )
        self.assertEqual(caught.exception.code, "BRAIN_PACKET_SENSITIVE_CONTENT_BLOCKED")
        self.assertNotIn("sk-proj-", str(caught.exception))

    def test_brain_packet_blocks_named_secret_material_variants(self):
        for content in (
            "access token: abcdefghijklmnop",
            "cookie=abcdef",
            "private credential: abcdef",
            "session secret=abcdef",
            "Authorization: Bearer abcdefghijklmnop",
        ):
            with self.subTest(content=content.split(":")[0]):
                with self.assertRaises(ManualGPTHandoffError) as caught:
                    self.transport.create_packet(
                        meeting_id="meeting-1", task_id="task-1", result_id="result-1",
                        brain_participant_id="brain-participant-1", decision_types_allowed=("ACCEPT",),
                        task_summary="Count files", task_instruction="Read-only task.",
                        agent_result=content, current_task_state="COMPLETED",
                        current_meeting_state="RUNNING",
                        validation_context={"taskStatus": "COMPLETED"},
                        safety_context={"circuitState": "CLOSED", "stopDispatch": False},
                    )
                self.assertEqual(caught.exception.code, "BRAIN_PACKET_SENSITIVE_CONTENT_BLOCKED")

    def test_valid_accept_decision(self):
        packet = self.packet()
        raw = json.dumps(self.decision_payload(packet))
        result = self.transport.validate_decision(raw, packet.data)
        self.assertEqual(result.decision_id, "decision-1")
        self.assertEqual(result.decision, "ACCEPT")
        self.assertEqual(result.task_id, packet.data["taskId"])
        self.assertEqual(result.result_id, packet.data["resultId"])

    def test_valid_rework_decision(self):
        packet = self.packet()
        payload = self.decision_payload(
            packet, decision="REWORK", reworkInstruction="List the filenames in sorted order."
        )
        result = self.transport.validate_decision(json.dumps(payload), packet.data)
        body = result.as_core_body(packet.data["brainParticipantId"])
        self.assertEqual(result.decision, "REWORK")
        self.assertEqual(body["relatedTaskId"], packet.data["taskId"])
        self.assertEqual(body["resultId"], packet.data["resultId"])
        self.assertEqual(body["instruction"], "List the filenames in sorted order.")

    def test_wrong_meeting_rejected(self):
        packet = self.packet()
        payload = self.decision_payload(packet, meetingId="meeting-elsewhere")
        with self.assertRaises(ManualGPTHandoffError) as caught:
            self.transport.validate_decision(json.dumps(payload), packet.data)
        self.assertEqual(caught.exception.code, "CROSS_MEETING_DECISION_REJECTED")

    def test_wrong_task_rejected(self):
        packet = self.packet()
        payload = self.decision_payload(packet, taskId="task-old")
        with self.assertRaises(ManualGPTHandoffError) as caught:
            self.transport.validate_decision(json.dumps(payload), packet.data)
        self.assertEqual(caught.exception.code, "STALE_DECISION_REJECTED")

    def test_wrong_result_rejected(self):
        packet = self.packet()
        payload = self.decision_payload(packet, resultId="result-old")
        with self.assertRaises(ManualGPTHandoffError) as caught:
            self.transport.validate_decision(json.dumps(payload), packet.data)
        self.assertEqual(caught.exception.code, "STALE_DECISION_REJECTED")

    def test_wrong_request_rejected(self):
        packet = self.packet()
        payload = self.decision_payload(packet, requestId="request-old")
        with self.assertRaises(ManualGPTHandoffError) as caught:
            self.transport.validate_decision(json.dumps(payload), packet.data)
        self.assertEqual(caught.exception.code, "INVALID_BRAIN_DECISION")

    def test_wrong_packet_rejected(self):
        packet = self.packet()
        payload = self.decision_payload(packet, packetId="packet-old")
        with self.assertRaises(ManualGPTHandoffError) as caught:
            self.transport.validate_decision(json.dumps(payload), packet.data)
        self.assertEqual(caught.exception.code, "INVALID_BRAIN_DECISION")

    def test_wrong_nonce_rejected(self):
        packet = self.packet()
        payload = self.decision_payload(packet, nonceEcho="nonce-old")
        with self.assertRaises(ManualGPTHandoffError) as caught:
            self.transport.validate_decision(json.dumps(payload), packet.data)
        self.assertEqual(caught.exception.code, "INVALID_BRAIN_DECISION")

    def test_missing_field_rejected(self):
        packet = self.packet()
        payload = self.decision_payload(packet)
        del payload["resultId"]
        with self.assertRaises(ManualGPTHandoffError) as caught:
            self.transport.validate_decision(json.dumps(payload), packet.data)
        self.assertEqual(caught.exception.code, "INVALID_BRAIN_DECISION")

    def test_invalid_json_rejected(self):
        packet = self.packet()
        with self.assertRaises(ManualGPTHandoffError) as caught:
            self.transport.validate_decision("not JSON", packet.data)
        self.assertEqual(caught.exception.code, "INVALID_BRAIN_DECISION")

    def test_multiple_decisions_rejected(self):
        packet = self.packet()
        raw = json.dumps([self.decision_payload(packet), self.decision_payload(packet)])
        with self.assertRaises(ManualGPTHandoffError) as caught:
            self.transport.validate_decision(raw, packet.data)
        self.assertEqual(caught.exception.code, "INVALID_BRAIN_DECISION")

    def test_duplicate_json_keys_rejected(self):
        packet = self.packet()
        raw = json.dumps(self.decision_payload(packet))[:-1] + ',"decisionId":"decision-shadowed"}'
        with self.assertRaises(ManualGPTHandoffError) as caught:
            self.transport.validate_decision(raw, packet.data)
        self.assertEqual(caught.exception.code, "INVALID_BRAIN_DECISION")


if __name__ == "__main__":
    unittest.main()
