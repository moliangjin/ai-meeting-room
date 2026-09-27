from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_meeting_room.core.service import MeetingCore
from ai_meeting_room.core.safety import DispatchBlockedError
from ai_meeting_room.models import AgentHealth, AgentRecord, AgentRole, AgentStatus, MeetingStatus
from ai_meeting_room.persistence.sqlite_store import SQLiteStore
from ai_meeting_room.product.app import Phase2Application
from ai_meeting_room.product.server import make_server


class BreakerRestartPersistenceTests(unittest.TestCase):
    @staticmethod
    def _create_paused_meeting(db_path: Path, meeting_id: str) -> SQLiteStore:
        store = SQLiteStore(db_path)
        core = MeetingCore.create("restart persistence", store, meeting_id=meeting_id)
        core.add_agent(
            AgentRecord(
                "healthy-agent",
                "Healthy Agent",
                "test",
                AgentRole.WORKER,
                status=AgentStatus.IDLE,
                health=AgentHealth.HEALTHY,
            )
        )
        core.mark_ready()
        core.start()
        core.pause("test pause", triggered_by="test")
        return store

    def test_paused_meeting_restart_restores_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.db"
            store = self._create_paused_meeting(db_path, "db05-paused")

            restored = MeetingCore.restore("db05-paused", store)

            self.assertEqual(restored.meeting.meeting.status, MeetingStatus.PAUSED)
            self.assertEqual(restored.safety.breaker.state.value, "OPEN")
            self.assertTrue(restored.safety.breaker.stop_dispatch)

    def test_stop_dispatch_true_after_paused_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.db"
            store = self._create_paused_meeting(db_path, "db05-stop-dispatch")

            restored = MeetingCore.restore("db05-stop-dispatch", store)

            self.assertTrue(restored.safety.breaker.snapshot()["stopDispatch"])
            self.assertEqual(store.get_circuit("db05-stop-dispatch")["stopDispatch"], True)

    def test_breaker_open_persists_across_restart(self) -> None:
        """An interrupted RUNNING meeting must remain fail-closed across boots."""
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.db"
            meeting_id = "db05-repeat-restart"
            store = SQLiteStore(db_path)
            core = MeetingCore.create("restart persistence", store, meeting_id=meeting_id)
            core.add_agent(AgentRecord("healthy-agent", "Healthy Agent", "test", AgentRole.WORKER, status=AgentStatus.IDLE, health=AgentHealth.HEALTHY))
            core.mark_ready()
            core.start()
            core._save_meeting()

            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import json, sys; "
                    "from ai_meeting_room.core.service import MeetingCore; "
                    "from ai_meeting_room.persistence.sqlite_store import SQLiteStore; "
                    "core=MeetingCore.restore(sys.argv[2], SQLiteStore(sys.argv[1])); "
                    "print(json.dumps({'meeting': core.meeting.meeting.status.value, "
                    "'breaker': core.safety.breaker.state.value, "
                    "'stopDispatch': core.safety.breaker.stop_dispatch}))",
                    str(db_path),
                    meeting_id,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            first_restart = json.loads(child.stdout)
            self.assertEqual(first_restart["meeting"], MeetingStatus.PAUSED.value)
            self.assertEqual(first_restart["breaker"], "OPEN")
            self.assertTrue(first_restart["stopDispatch"])

            # This new process observes the durable state left by the previous
            # restore process; the accepted behavior is still OPEN/true.
            second_restart = MeetingCore.restore(meeting_id, SQLiteStore(db_path))
            self.assertEqual(second_restart.meeting.meeting.status, MeetingStatus.PAUSED)
            self.assertEqual(second_restart.safety.breaker.state.value, "OPEN")
            self.assertTrue(second_restart.safety.breaker.stop_dispatch)

    def test_missing_breaker_state_with_paused_meeting_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.db"
            meeting_id = "db05-missing-breaker"
            store = self._create_paused_meeting(db_path, meeting_id)
            with sqlite3.connect(db_path) as db:
                db.execute("DELETE FROM circuit_breakers WHERE meeting_id=?", (meeting_id,))

            restored = MeetingCore.restore(meeting_id, store)

            self.assertEqual(restored.meeting.meeting.status, MeetingStatus.PAUSED)
            self.assertEqual(restored.safety.breaker.state.value, "OPEN")
            self.assertTrue(restored.safety.breaker.stop_dispatch)
            self.assertEqual(store.get_circuit(meeting_id)["circuitState"], "OPEN")

    def test_corrupt_breaker_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.db"
            meeting_id = "db05-corrupt-breaker"
            store = self._create_paused_meeting(db_path, meeting_id)
            with sqlite3.connect(db_path) as db:
                db.execute("UPDATE circuit_breakers SET data=? WHERE meeting_id=?", ("{not-json", meeting_id))

            restored = MeetingCore.restore(meeting_id, store)

            self.assertEqual(restored.meeting.meeting.status, MeetingStatus.PAUSED)
            self.assertEqual(restored.safety.breaker.state.value, "OPEN")
            self.assertTrue(restored.safety.breaker.stop_dispatch)
            self.assertIn("JSONDecodeError", str(store.get_circuit(meeting_id)["triggerReason"]))

    def test_corrupt_breaker_shape_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.db"
            meeting_id = "db05-corrupt-shape"
            store = self._create_paused_meeting(db_path, meeting_id)
            with sqlite3.connect(db_path) as db:
                db.execute(
                    "UPDATE circuit_breakers SET data=? WHERE meeting_id=?",
                    (json.dumps({"circuitState": "UNKNOWN", "interruptErrors": "invalid-shape"}), meeting_id),
                )

            restored = MeetingCore.restore(meeting_id, store)

            self.assertEqual(restored.meeting.meeting.status, MeetingStatus.PAUSED)
            self.assertEqual(restored.safety.breaker.state.value, "OPEN")
            self.assertTrue(restored.safety.breaker.stop_dispatch)

    def test_recovering_restart_stays_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.db"
            meeting_id = "db05-recovering"
            store = self._create_paused_meeting(db_path, meeting_id)
            paused = MeetingCore.restore(meeting_id, store)
            paused.meeting.transition(MeetingStatus.RECOVERING, reason="recovery entered")
            paused._save_meeting()
            paused.safety.breaker.begin_recovery()

            restored = MeetingCore.restore(meeting_id, store)

            self.assertEqual(restored.meeting.meeting.status, MeetingStatus.PAUSED)
            self.assertEqual(restored.safety.breaker.state.value, "OPEN")
            self.assertTrue(restored.safety.breaker.stop_dispatch)

    def test_restart_does_not_auto_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.db"
            meeting_id = "db05-no-auto-resume"
            store = SQLiteStore(db_path)
            running = MeetingCore.create("no auto resume", store, meeting_id=meeting_id)
            running.add_agent(AgentRecord("a", "A", "test", AgentRole.WORKER, status=AgentStatus.IDLE, health=AgentHealth.HEALTHY))
            running.mark_ready()
            running.start()
            running._save_meeting()

            restored = MeetingCore.restore(meeting_id, SQLiteStore(db_path))

            self.assertEqual(restored.meeting.meeting.status, MeetingStatus.PAUSED)
            self.assertEqual(restored.safety.breaker.state.value, "OPEN")
            self.assertTrue(restored.safety.breaker.stop_dispatch)

    def test_restart_recovery_gate_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.db"
            meeting_id = "db05-recovery-gate"
            store = self._create_paused_meeting(db_path, meeting_id)

            restored = MeetingCore.restore(meeting_id, store)
            result = restored.recover(
                cao_health_check=lambda: True,
                workspace_health_check=lambda: True,
            )

            self.assertFalse(result)
            self.assertEqual(restored.meeting.meeting.status, MeetingStatus.PAUSED)
            self.assertEqual(restored.safety.breaker.state.value, "OPEN")
            self.assertTrue(restored.safety.breaker.stop_dispatch)

    def test_api_not_ready_for_dispatch_before_safety_hydration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.db"
            meeting_id = "db05-api-barrier"
            store = self._create_paused_meeting(db_path, meeting_id)
            app = Phase2Application(store, adapter_factory=lambda **_: None)
            self.assertFalse(app.safety_state_hydrated)

            listener_construction_states: list[bool] = []

            class ListenerBoundary:
                def __init__(self, _address, _handler) -> None:
                    listener_construction_states.append(app.safety_state_hydrated)

                def server_close(self) -> None:
                    return None

            with patch("ai_meeting_room.product.server.HTTPServer", ListenerBoundary):
                server = make_server(app, host="127.0.0.1", port=0)
                self.assertTrue(app.safety_state_hydrated)
                self.assertEqual(listener_construction_states, [True])
                core = app._cores[meeting_id]
                self.assertEqual(core.meeting.meeting.status, MeetingStatus.PAUSED)
                self.assertEqual(core.safety.breaker.state.value, "OPEN")
                self.assertTrue(core.safety.breaker.stop_dispatch)
                with self.assertRaises(DispatchBlockedError):
                    app.create_task(meeting_id, "must be rejected", "read-only")
                self.assertEqual(store.list_tasks(meeting_id), [])
                server.server_close()
            app.close()


if __name__ == "__main__":
    unittest.main()
