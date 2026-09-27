from __future__ import annotations

import tempfile
import json
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from ai_meeting_room.agents.adapter import AgentAdapter
from ai_meeting_room.models import AgentHealth, MeetingStatus
from ai_meeting_room.persistence.sqlite_store import SQLiteStore
from ai_meeting_room.product.app import Phase2Application
from ai_meeting_room.product.server import UI_HTML, make_server


class ProductizationAdapter(AgentAdapter):
    def __init__(self) -> None:
        self.agent_id = "phase3c-productization-agent"
        self.stopped = False
        self.raw_status = "idle"
        self.output = ""

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
        self.raw_status = "completed"
        self.output = "Fixture read-only result: 2 files"
        return task["task_id"]

    @property
    def task_completion_observed(self) -> bool:
        return self.raw_status == "completed" and bool(self.output)

    def get_status(self) -> str:
        # The runtime exposes completion through raw_status/output; its health
        # status remains IDLE until the watcher records the task result.
        return "IDLE"

    def get_health(self) -> str:
        return AgentHealth.HEALTHY.value

    def get_output(self) -> str:
        return self.output

    def health_check(self) -> bool:
        return not self.stopped


class FakeCaoServiceManager:
    def __init__(self) -> None:
        self.calls = 0

    def recover(self) -> dict[str, object]:
        self.calls += 1
        return {
            "status": "READY", "action": "STARTED_BY_PRODUCT",
            "ownershipMode": "PRODUCT_RUNTIME_MANAGED_PERSISTENT",
            "terminalBackend": "tmux", "processPid": 4242, "persistent": True,
        }


class Phase3CProductizationTests(unittest.TestCase):
    def test_product_shell_exposes_the_product_workflow_contract(self) -> None:
        required_markers = (
            "主控面板",
            "生命周期",
            "开始会议",
            "暂停会议",
            "恢复会议",
            "完成会议",
            "Brain Packet",
            "等待粘贴 GPT 决策",
            "校验决策",
            "决策预览",
            "应用决策",
            "安全状态",
            "运行时预检",
            "审计时间线",
            "诊断详情",
            "GPT 网页自动化：实验功能",
            "/api/runtime/preflight",
            "/api/runtime/cao/recover",
            "启动 / 重试本机 CAO",
            "/complete",
            "ACCEPT 与完成会议完全分离",
        )
        for marker in required_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, UI_HTML)
        self.assertNotIn("navigator.clipboard.readText", UI_HTML)
        self.assertIn("navigator.clipboard?.writeText", UI_HTML)
        self.assertNotIn("/api/dev/", UI_HTML)

    def test_complete_meeting_uses_core_semantics_and_is_not_accept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = Phase2Application(
                SQLiteStore(Path(directory) / "state.db"),
                adapter_factory=lambda **_: ProductizationAdapter(),
            )
            try:
                meeting_id = app.create_meeting("Phase 3C", directory)["meeting"]["meeting_id"]
                app.join_provider(meeting_id, "codex")
                app.start_meeting(meeting_id)
                completed = app.complete_meeting(meeting_id, reason="operator completed meeting")
                self.assertEqual(completed["meeting"]["status"], MeetingStatus.COMPLETED.value)
                self.assertTrue(completed["meeting"]["completed_at"])
            finally:
                app.close()

    def test_runtime_preflight_shape_is_explicit_and_non_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = Phase2Application(
                SQLiteStore(Path(directory) / "state.db"),
                adapter_factory=lambda **_: ProductizationAdapter(),
            )
            try:
                result = app.product_runtime_preflight()
                self.assertIn(result["overallStatus"], {"READY", "DEGRADED", "BLOCKED"})
                self.assertIn("productShell", result["checks"])
                self.assertIn("database", result["checks"])
                self.assertIn("cao", result["checks"])
                self.assertIn("tmux", result["checks"])
                self.assertIn("codex", result["checks"])
                self.assertIn("codexAuth", result["checks"])
                self.assertNotIn("credential", result)
                self.assertNotIn("token", result)
            finally:
                app.close()

    def test_product_shell_http_routes_use_formal_core_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = Phase2Application(
                SQLiteStore(Path(directory) / "state.db"),
                adapter_factory=lambda **_: ProductizationAdapter(),
            )
            server = make_server(app, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                meeting_id = app.create_meeting("HTTP Product Shell", directory)["meeting"]["meeting_id"]
                app.join_provider(meeting_id, "codex")
                app.start_meeting(meeting_id)

                with urllib.request.urlopen(f"{base}/") as response:
                    html = response.read().decode("utf-8")
                self.assertIn("主控面板", html)
                self.assertIn("完成会议", html)

                with urllib.request.urlopen(f"{base}/api/runtime/preflight") as response:
                    preflight = json.loads(response.read().decode("utf-8"))
                self.assertIn(preflight["overallStatus"], {"READY", "DEGRADED", "BLOCKED"})

                request = urllib.request.Request(
                    f"{base}/api/meetings/{meeting_id}/complete",
                    data=json.dumps({"reason": "HTTP lifecycle acceptance"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    completed = json.loads(response.read().decode("utf-8"))
                self.assertEqual(completed["meeting"]["status"], MeetingStatus.COMPLETED.value)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                app.close()

    def test_local_runtime_recovery_ui_action_uses_product_cao_manager(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = FakeCaoServiceManager()
            app = Phase2Application(
                SQLiteStore(Path(directory) / "state.db"),
                adapter_factory=lambda **_: ProductizationAdapter(),
                cao_service_manager=manager,
            )
            server = make_server(app, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urllib.request.urlopen(base) as response:
                    html = response.read().decode("utf-8")
                self.assertIn("/api/runtime/cao/recover", html)
                request = urllib.request.Request(
                    f"{base}/api/runtime/cao/recover", data=b"{}",
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    recovered = json.loads(response.read().decode("utf-8"))
                self.assertEqual(recovered["action"], "STARTED_BY_PRODUCT")
                self.assertEqual(recovered["terminalBackend"], "tmux")
                self.assertEqual(manager.calls, 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                app.close()

    def test_fresh_product_smoke_reaches_waiting_human_handoff(self) -> None:
        """C31 smoke: fresh Meeting → joins → Start → fixture task → WAITING."""
        with tempfile.TemporaryDirectory() as directory:
            app = Phase2Application(
                SQLiteStore(Path(directory) / "state.db"),
                adapter_factory=lambda **_: ProductizationAdapter(),
            )
            try:
                meeting_id = app.create_meeting("Fresh Phase 3C smoke", directory)["meeting"]["meeting_id"]
                joined = app.join_provider(meeting_id, "codex")
                agent_id = joined["agents"][0]["agent_id"]
                app.start_meeting(meeting_id)
                task_id = app.create_task(meeting_id, "Fixture read-only task", "Return the fixture result only.", agent_id)["tasks"][-1]["task_id"]
                app.dispatch_task(meeting_id, task_id)

                deadline = time.monotonic() + 2
                snapshot = app.snapshot(meeting_id)
                while time.monotonic() < deadline:
                    snapshot = app.snapshot(meeting_id)
                    task = next(item for item in snapshot["tasks"] if item["task_id"] == task_id)
                    if task["status"] == "COMPLETED" and snapshot.get("brainHandoff"):
                        break
                    time.sleep(0.02)

                self.assertEqual(snapshot["meeting"]["status"], MeetingStatus.RUNNING.value)
                self.assertTrue(snapshot["brainParticipant"]["participantId"])
                self.assertEqual(snapshot["agents"][0]["provider"], "codex")
                self.assertEqual(next(item for item in snapshot["tasks"] if item["task_id"] == task_id)["status"], "COMPLETED")
                self.assertEqual(snapshot["brainHandoff"]["status"], "WAITING_FOR_HUMAN_HANDOFF")
                self.assertEqual(snapshot["brain"]["state"], "WAITING_FOR_HUMAN_HANDOFF")
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
