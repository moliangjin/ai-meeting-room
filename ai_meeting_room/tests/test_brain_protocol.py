from __future__ import annotations

import unittest

from ai_meeting_room.brain.protocol import BrainDecisionInvalid, BrainRequestIdempotencyGuard, parse_brain_decision


class BrainProtocolTests(unittest.TestCase):
    request_id = "req-1"
    task_id = "task-1"

    def valid(self, decision: str = "ACCEPT", task_id: str = task_id) -> str:
        return ('{"decision":"%s","taskId":"%s","reason":"reviewed","instruction":"%s",'
                '"confidence":0.9}' % (decision, task_id, "redo" if decision == "REWORK" else ""))

    def test_valid_accept(self) -> None:
        result = parse_brain_decision(self.valid(), brain_request_id=self.request_id, expected_task_id=self.task_id)
        self.assertEqual(result.decision, "ACCEPT")

    def test_valid_rework(self) -> None:
        result = parse_brain_decision(self.valid("REWORK"), brain_request_id=self.request_id, expected_task_id=self.task_id)
        self.assertEqual(result.instruction, "redo")

    def test_valid_complete_meeting(self) -> None:
        result = parse_brain_decision(self.valid("COMPLETE_MEETING"), brain_request_id=self.request_id, expected_task_id=self.task_id)
        self.assertEqual(result.decision, "COMPLETE_MEETING")

    def test_markdown_fenced_json_is_supported(self) -> None:
        raw = "```json\n" + self.valid() + "\n```"
        self.assertEqual(parse_brain_decision(raw, brain_request_id=self.request_id, expected_task_id=self.task_id).decision, "ACCEPT")

    def test_invalid_inputs_fail_closed(self) -> None:
        invalid = [
            self.valid("UNKNOWN"),
            '{"decision":"ACCEPT","reason":"missing task","instruction":"","confidence":1}',
            "not json",
            "",
            "prose " + self.valid(),
            self.valid(task_id="wrong-task"),
            self.valid().replace('"confidence":0.9', '"confidence":2'),
            self.valid().replace('"instruction":""', '"instruction":null'),
            self.valid().replace('"confidence":0.9', '"confidence":true'),
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(BrainDecisionInvalid):
                    parse_brain_decision(raw, brain_request_id=self.request_id, expected_task_id=self.task_id)

    def test_duplicate_request_id_is_rejected(self) -> None:
        guard = BrainRequestIdempotencyGuard()
        self.assertTrue(guard.claim(self.request_id))
        self.assertFalse(guard.claim(self.request_id))
        self.assertTrue(guard.seen(self.request_id))


if __name__ == "__main__":
    unittest.main()
