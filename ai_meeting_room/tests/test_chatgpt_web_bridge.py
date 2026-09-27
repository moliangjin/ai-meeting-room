from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from ai_meeting_room.agents.adapter import AgentAdapter
from ai_meeting_room.brain.chatgpt_web import BrowserController, BrainBridgeState
from ai_meeting_room.core.safety import DispatchBlockedError
from ai_meeting_room.models import AgentHealth
from ai_meeting_room.persistence.sqlite_store import SQLiteStore
from ai_meeting_room.product.app import Phase2Application


class ScriptedBrowser(BrowserController):
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[str] = []
        self.task_id = ""

    def connect(self) -> None: pass
    def auth_status(self) -> str: return "AUTHENTICATED"
    def open_conversation(self, conversation_id=None) -> str: return "poc-conversation"

    def send_prompt(self, prompt: str) -> None:
        self.sent.append(prompt)
        marker = "taskId="
        self.task_id = prompt.split(marker, 1)[1].split("\n", 1)[0]

    def wait_for_completion(self, timeout_seconds: float) -> None:
        if self.fail:
            raise TimeoutError("browser response timeout")

    def read_response(self) -> str:
        return json.dumps({"decision": "ACCEPT", "taskId": self.task_id, "reason": "read-only result accepted", "instruction": "", "confidence": 1.0})

    def close(self) -> None: pass


class FastAdapter(AgentAdapter):
    def __init__(self) -> None:
        self.agent_id = "agent-runtime"
        self.raw_status = "idle"
        self.output = ""

    def start(self) -> str: return self.agent_id
    def stop(self) -> None: self.raw_status = "stopped"
    def pause(self) -> None: pass
    def resume(self) -> None: pass
    def interrupt(self) -> None: self.raw_status = "idle"
    def send_task(self, task): self.raw_status = "completed"; self.output = "1 file: README.md"; return task["task_id"]
    @property
    def task_completion_observed(self) -> bool: return self.raw_status == "completed" and bool(self.output)
    def get_status(self) -> str: return "IDLE"
    def get_health(self) -> str: return AgentHealth.HEALTHY.value
    def get_output(self) -> str: return self.output
    def health_check(self) -> bool: return self.raw_status != "stopped"


class ChatGPTWebBridgeTests(unittest.TestCase):
    def make_app(self, directory: str) -> Phase2Application:
        return Phase2Application(SQLiteStore(Path(directory) / "state.db"), adapter_factory=lambda **_: FastAdapter())

    def prepare_completed_task(self, app: Phase2Application, directory: str) -> tuple[str, str]:
        meeting_id = app.create_meeting("web brain", directory)["meeting"]["meeting_id"]
        agent_id = app.join_provider(meeting_id, "codex")["agents"][0]["agent_id"]
        app.start_meeting(meeting_id)
        task_id = app.create_task(meeting_id, "count", "List filenames only.", agent_id)["tasks"][-1]["task_id"]
        app.dispatch_task(meeting_id, task_id)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and app.snapshot(meeting_id)["tasks"][-1]["status"] != "COMPLETED":
            time.sleep(0.01)
        return meeting_id, task_id

    def test_web_review_is_validated_then_submitted_to_core(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            try:
                meeting_id, task_id = self.prepare_completed_task(app, directory)
                browser = ScriptedBrowser()
                app.attach_chatgpt_web_bridge(meeting_id, browser)
                result = app.run_chatgpt_review(meeting_id, task_id)
                self.assertTrue(result["tasks"][-1]["accepted"])
                self.assertEqual(app._brains[meeting_id].state, BrainBridgeState.READY)
                self.assertEqual(len(browser.sent), 1)
                self.assertTrue(app.snapshot(meeting_id)["brainDecisions"])
            finally:
                app.close()

    def test_web_bridge_failure_opens_brain_global_pause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            try:
                meeting_id, task_id = self.prepare_completed_task(app, directory)
                app.attach_chatgpt_web_bridge(meeting_id, ScriptedBrowser(fail=True))
                with self.assertRaises(TimeoutError):
                    app.run_chatgpt_review(meeting_id, task_id)
                snapshot = app.snapshot(meeting_id)
                self.assertEqual(snapshot["meeting"]["status"], "PAUSED")
                self.assertTrue(snapshot["circuit"]["stopDispatch"])
                self.assertEqual(snapshot["circuit"]["participantType"], "BRAIN")
                self.assertIn("BRAIN_UNAVAILABLE", snapshot["circuit"]["triggerReason"])
                with self.assertRaises(DispatchBlockedError):
                    app.create_task(meeting_id, "blocked", "must not dispatch", snapshot["agents"][0]["agent_id"])
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
