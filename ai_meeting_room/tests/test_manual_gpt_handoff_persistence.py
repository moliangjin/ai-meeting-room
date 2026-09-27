from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_meeting_room.brain.manual_gpt import ManualGPTBrainTransport, WAITING_FOR_HUMAN_HANDOFF
from ai_meeting_room.persistence.sqlite_store import SQLiteStore


class ManualGPTHandoffPersistenceTests(unittest.TestCase):
    def test_pending_brain_request_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "meeting.db"
            store = SQLiteStore(database)
            packet = ManualGPTBrainTransport().create_packet(
                meeting_id="meeting-1", task_id="task-1", result_id="result-1",
                brain_participant_id="brain-participant-1", decision_types_allowed=("ACCEPT",),
                task_summary="Count files", task_instruction="List filenames.", agent_result="2 files.",
                current_task_state="COMPLETED", current_meeting_state="RUNNING",
                validation_context={"taskStatus": "COMPLETED", "taskAccepted": False},
                safety_context={"circuitState": "CLOSED", "stopDispatch": False},
            )
            record = {
                "packet": packet.data,
                "copyText": packet.copy_text,
                "status": WAITING_FOR_HUMAN_HANDOFF,
                "validationStatus": "NOT_IMPORTED",
                "stagedDecision": None,
                "consumedDecisionId": None,
            }
            store.save_manual_gpt_handoff_request(record)

            restarted_store = SQLiteStore(database)
            restored = restarted_store.get_manual_gpt_handoff_request(packet.data["requestId"])
            self.assertEqual(restored["packet"], packet.data)
            self.assertEqual(restored["copyText"], packet.copy_text)
            self.assertEqual(restored["status"], WAITING_FOR_HUMAN_HANDOFF)
            self.assertEqual(restored["consumedDecisionId"], None)


if __name__ == "__main__":
    unittest.main()
