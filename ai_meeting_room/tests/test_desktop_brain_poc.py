from __future__ import annotations

import json
import unittest
from pathlib import Path

from ai_meeting_room.brain.desktop import validate_desktop_brain_poc
from ai_meeting_room.brain.protocol import BrainDecisionInvalid


class DesktopBrainPocTests(unittest.TestCase):
    def valid_raw(self) -> str:
        return json.dumps({
            "decision": "ACCEPT",
            "taskId": "desktop-brain-poc",
            "reason": "desktop brain poc",
            "instruction": "",
            "confidence": 1.0,
        })

    def test_desktop_brain_poc_task_id_validation(self) -> None:
        result = validate_desktop_brain_poc(self.valid_raw(), brain_request_id="r-1")
        self.assertTrue(result["validated"])
        self.assertEqual(result["decision"]["taskId"], "desktop-brain-poc")
        with self.assertRaises(BrainDecisionInvalid):
            validate_desktop_brain_poc(self.valid_raw().replace("desktop-brain-poc", "other-task"), brain_request_id="r-1")

    def test_transport_cannot_directly_mutate_core(self) -> None:
        source = Path(__file__).parents[2].joinpath("desktop", "electron_transport.js").read_text()
        self.assertNotIn("MeetingCore", source)
        self.assertNotIn("TaskEngine", source)
        self.assertNotIn("BrainInbox", source)


if __name__ == "__main__":
    unittest.main()
