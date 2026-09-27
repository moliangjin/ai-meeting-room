from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from ai_meeting_room.agents.adapter import AgentAdapter
from ai_meeting_room.brain.chatgpt_web import BrainBridgeState, BrainRequest, BrowserController, ChatGPTWebBrainBridge
from ai_meeting_room.brain.runtime_registry import FormalBrainRuntimeRegistry, FormalBrainRuntimeService
from ai_meeting_room.models import AgentHealth, AgentRecord, AgentRole, AgentStatus
from ai_meeting_room.product.app import Phase2Application
from ai_meeting_room.persistence.sqlite_store import SQLiteStore


class _RuntimeError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _AuthController(BrowserController):
    def __init__(self, auth_state: str, *, connect_error: Exception | None = None, readiness: dict | None = None, send_error: Exception | None = None, wait_error: Exception | None = None, response: str = "") -> None:
        self.auth_state = auth_state
        self.connect_error = connect_error
        self.readiness = readiness
        self.send_error = send_error
        self.wait_error = wait_error
        self.response = response

    def connect(self) -> None:
        if self.connect_error:
            raise self.connect_error

    def auth_status(self) -> str:
        return self.auth_state

    def open_conversation(self, conversation_id: str | None = None) -> str:
        return "conversation"

    def send_prompt(self, prompt: str) -> None:
        if self.send_error:
            raise self.send_error

    def wait_for_completion(self, timeout_seconds: float) -> None:
        if self.wait_error:
            raise self.wait_error

    def read_response(self) -> str:
        return self.response

    def live_poc_health_check(self) -> dict | None:
        return self.readiness

    def close(self) -> None:
        pass


class Phase3AAuthClassificationTests(unittest.TestCase):
    def test_explicit_login_ui_maps_auth_required(self) -> None:
        bridge = ChatGPTWebBrainBridge(_AuthController("AUTH_REQUIRED"))

        bridge.start()

        self.assertEqual(bridge.state, BrainBridgeState.AUTH_REQUIRED)
        self.assertEqual(bridge.auth_state, "AUTH_REQUIRED")

    def test_authenticated_page_maps_authenticated(self) -> None:
        bridge = ChatGPTWebBrainBridge(_AuthController("AUTHENTICATED"))

        bridge.start()

        self.assertEqual(bridge.state, BrainBridgeState.READY)
        self.assertEqual(bridge.auth_state, "AUTHENTICATED")

    def test_unknown_dom_does_not_map_to_auth_required(self) -> None:
        bridge = ChatGPTWebBrainBridge(_AuthController("DOM_UNKNOWN"))

        bridge.start()

        self.assertEqual(bridge.state, BrainBridgeState.AUTH_UNKNOWN)

    def test_browser_lost_does_not_map_to_auth_required(self) -> None:
        bridge = ChatGPTWebBrainBridge(_AuthController("UNKNOWN", connect_error=_RuntimeError("BROWSER_LOST")))

        bridge.start()

        self.assertEqual(bridge.state, BrainBridgeState.LOST)
        self.assertNotEqual(bridge.state, BrainBridgeState.AUTH_REQUIRED)

    def test_composer_missing_does_not_map_to_auth_required(self) -> None:
        bridge = ChatGPTWebBrainBridge(_AuthController("AUTHENTICATED", readiness={
            "browserConnected": True,
            "pageAlive": True,
            "authState": "AUTHENTICATED",
            "composerReady": False,
            "precheckResult": "FAIL",
            "failureCode": "COMPOSER_NOT_FOUND",
        }))

        bridge.start()

        self.assertEqual(bridge.state, BrainBridgeState.ERROR)
        self.assertEqual(bridge.auth_state, "AUTHENTICATED")

    def test_composer_write_failure_preserves_authenticated(self) -> None:
        controller = _AuthController("AUTHENTICATED", send_error=_RuntimeError("COMPOSER_WRITE_FAILED"))
        bridge = ChatGPTWebBrainBridge(controller)
        bridge.start()

        with self.assertRaisesRegex(_RuntimeError, "COMPOSER_WRITE_FAILED"):
            bridge.dispatch_instruction({"request": _request(), "reuseCurrentPage": True})

        self.assertEqual(bridge.auth_state, "AUTHENTICATED")
        self.assertEqual(controller.auth_status(), "AUTHENTICATED")

    def test_response_timeout_preserves_authenticated(self) -> None:
        controller = _AuthController("AUTHENTICATED", wait_error=TimeoutError("response timeout"))
        bridge = ChatGPTWebBrainBridge(controller)
        bridge.start()

        with self.assertRaises(TimeoutError):
            bridge.dispatch_instruction({"request": _request(), "reuseCurrentPage": True})

        self.assertEqual(bridge.auth_state, "AUTHENTICATED")

    def test_parser_failure_preserves_authenticated(self) -> None:
        controller = _AuthController("AUTHENTICATED", response="not-json")
        bridge = ChatGPTWebBrainBridge(controller)
        bridge.start()

        with self.assertRaises(Exception):
            bridge.dispatch_instruction({"request": _request(), "reuseCurrentPage": True})

        self.assertEqual(bridge.auth_state, "AUTHENTICATED")


class _FormalHost(_AuthController):
    def __init__(self, name: str = "host-a") -> None:
        super().__init__("AUTHENTICATED")
        self.host_instance_id = name
        self.registry_instance_id = "registry-unassigned"
        self.owner_thread_id = 17
        self.bound_page_id = "page-0"
        self.browser_connected = True
        self.page_alive = True
        self.composer_ready = True
        self.on_failure = None

    def openBrain(self, *, connect_attempt_id=None) -> dict:
        return self.status()

    def runtime_identity_snapshot(self) -> dict:
        return {
            "registryInstanceId": self.registry_instance_id,
            "hostInstanceId": self.host_instance_id,
            "boundPageId": self.bound_page_id,
            "ownerThreadId": self.owner_thread_id,
            "processPid": 123,
        }

    def live_poc_health_check(self) -> dict:
        ready = self.browser_connected and self.page_alive and self.auth_state == "AUTHENTICATED" and self.composer_ready
        return {
            "browserConnected": self.browser_connected,
            "pageAlive": self.page_alive,
            "hostname": "chatgpt.com" if self.page_alive else None,
            "authState": self.auth_state if self.page_alive else "UNKNOWN",
            "composerReady": self.composer_ready,
            "precheckResult": "PASS" if ready else "FAIL",
            "failureCode": None if ready else "BROWSER_LOST",
            "boundPageId": self.bound_page_id,
        }

    def status(self) -> dict:
        return {
            "state": "READY" if self.live_poc_health_check()["precheckResult"] == "PASS" else "ERROR",
            "authState": self.auth_state,
            "browserConnected": self.browser_connected,
            "boundPageId": self.bound_page_id,
            "composerFound": self.composer_ready,
        }

    def stop(self, **_kwargs) -> None:
        pass

    def close(self, **_kwargs) -> None:
        pass


class _HealthyAgentAdapter(AgentAdapter):
    agent_id = "runtime-test-worker"
    def start(self) -> str: return "test-worker"
    def stop(self) -> None: pass
    def pause(self) -> None: pass
    def resume(self) -> None: pass
    def interrupt(self) -> None: pass
    def send_task(self, task) -> str: return str(task["task_id"])
    def get_status(self) -> str: return "IDLE"
    def get_health(self) -> str: return AgentHealth.HEALTHY.value
    def get_output(self) -> str: return ""
    def health_check(self) -> bool: return True


class Phase3ABrainParticipantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Phase2Application(
            SQLiteStore(self.root / "state.db"),
            adapter_factory=lambda **_kwargs: None,
        )
        self.host_a = _FormalHost()
        self._install_host(self.host_a)
        self.host_a.on_failure = lambda state, reason: self.app._on_real_chrome_brain_failure(
            state, reason, source_host=self.host_a
        )

    def tearDown(self) -> None:
        self.app.close()
        self.temp.cleanup()

    def _install_host(self, host: _FormalHost) -> None:
        registry = FormalBrainRuntimeRegistry(host, runtime_owner="PRODUCT_SHELL")
        self.app.formal_brain_registry = registry
        self.app.formal_brain_service = FormalBrainRuntimeService(registry)
        self.app.real_chrome_brain = host

    def _meeting(self, name: str) -> str:
        meeting_id = self.app.create_meeting(name, str(self.root))["meeting"]["meeting_id"]
        self.app._core(meeting_id).add_agent(
            AgentRecord(
                agent_id=f"test-worker-{meeting_id[:8]}",
                display_name="Test Worker",
                provider="test",
                role=AgentRole.WORKER,
                status=AgentStatus.IDLE,
                health=AgentHealth.HEALTHY,
            ),
            _HealthyAgentAdapter(),
        )
        return meeting_id

    def _join_formal(self, meeting_id: str) -> dict:
        return self.app.join_formal_brain(meeting_id)

    def _fail_host(self, host: _FormalHost, state: str = "BROWSER_LOST") -> None:
        host.browser_connected = False
        host.page_alive = False
        host.auth_state = "UNKNOWN"
        host.composer_ready = False
        host.on_failure(state, "injected health failure")

    def _recover(self, meeting_id: str) -> dict:
        class _Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_args): return False

        with patch("ai_meeting_room.product.app.urllib.request.urlopen", return_value=_Response()), patch(
            "ai_meeting_room.product.app.WorkspaceManager"
        ) as workspace_manager:
            workspace_manager.return_value.health_check.return_value = True
            return self.app.recover(meeting_id)

    def test_joined_brain_failure_pauses_only_meeting_bound_to_runtime(self) -> None:
        joined_id = self._meeting("joined brain")
        unjoined_id = self._meeting("unjoined brain")
        joined = self._join_formal(joined_id)
        self.app.start_meeting(joined_id)
        self.app.start_meeting(unjoined_id)

        self._fail_host(self.host_a)

        self.assertEqual(joined["brainParticipant"]["provider"], "GPT_WEB_BRAIN")
        self.assertEqual(self.app.snapshot(joined_id)["meeting"]["status"], "PAUSED")
        self.assertEqual(self.app.snapshot(unjoined_id)["meeting"]["status"], "RUNNING")

    def test_unjoined_brain_failure_does_not_pause_meeting(self) -> None:
        meeting_id = self._meeting("manual brain only")
        self.app.start_meeting(meeting_id)

        self._fail_host(self.host_a)

        self.assertEqual(self.app.snapshot(meeting_id)["meeting"]["status"], "RUNNING")

    def test_brain_failure_event_is_bound_to_meeting_participant_and_runtime(self) -> None:
        meeting_id = self._meeting("identity event")
        joined = self._join_formal(meeting_id)
        self.app.start_meeting(meeting_id)

        self._fail_host(self.host_a, "UNKNOWN")

        circuit = self.app.snapshot(meeting_id)["circuit"]
        self.assertEqual(circuit["brainParticipantId"], joined["brainParticipant"]["participantId"])
        self.assertEqual(circuit["brainRuntimeIdentity"]["hostInstanceId"], self.host_a.host_instance_id)
        events = [item for item in self.app.store.list_events(meeting_id) if item["event_type"] == "BrainCircuitBreakerOpened"]
        self.assertEqual(len(events), 1)
        payload = json.loads(events[0]["payload"])
        self.assertEqual(payload["brainParticipantId"], joined["brainParticipant"]["participantId"])
        self.assertEqual(payload["brainRuntimeIdentity"]["registryInstanceId"], joined["brainParticipant"]["runtimeIdentity"]["registryInstanceId"])

    def test_ui_projects_authoritative_auth_state(self) -> None:
        self.host_a.auth_state = "AUTHENTICATED"

        status = self.app.real_chrome_brain_status()

        self.assertEqual(status["authState"], "AUTHENTICATED")

    def test_recovery_checks_the_same_brain_participant(self) -> None:
        meeting_id = self._meeting("same brain recovery")
        joined = self._join_formal(meeting_id)
        self.app.start_meeting(meeting_id)
        self._fail_host(self.host_a)
        participant_id = joined["brainParticipant"]["participantId"]

        self.host_a.browser_connected = True
        self.host_a.page_alive = True
        self.host_a.auth_state = "AUTHENTICATED"
        self.host_a.composer_ready = True
        result = self._recover(meeting_id)

        self.assertTrue(result["recovered"])
        self.assertEqual(result["brainParticipant"]["participantId"], participant_id)
        self.assertEqual(result["meeting"]["status"], "RUNNING")

    def test_other_ready_brain_cannot_satisfy_failed_participant_recovery(self) -> None:
        meeting_id = self._meeting("other brain recovery")
        self._join_formal(meeting_id)
        self.app.start_meeting(meeting_id)
        self._fail_host(self.host_a)
        host_b = _FormalHost("host-b")
        self._install_host(host_b)

        result = self._recover(meeting_id)

        self.assertFalse(result["recovered"])
        self.assertEqual(result["meeting"]["status"], "PAUSED")

    def test_stale_brain_health_cannot_satisfy_recovery(self) -> None:
        meeting_id = self._meeting("stale brain recovery")
        self._join_formal(meeting_id)
        self.app.start_meeting(meeting_id)
        self._fail_host(self.host_a)
        self.host_a.browser_connected = True
        self.host_a.page_alive = True
        self.host_a.auth_state = "AUTHENTICATED"
        self.host_a.composer_ready = True
        self.host_a.bound_page_id = "page-replaced"

        result = self._recover(meeting_id)

        self.assertFalse(result["recovered"])
        self.assertEqual(result["meeting"]["status"], "PAUSED")

    def test_joined_brain_unknown_is_fail_closed(self) -> None:
        meeting_id = self._meeting("unknown brain failure")
        self._join_formal(meeting_id)
        self.app.start_meeting(meeting_id)

        self._fail_host(self.host_a, "UNKNOWN")

        snapshot = self.app.snapshot(meeting_id)
        self.assertEqual(snapshot["meeting"]["status"], "PAUSED")
        self.assertTrue(snapshot["circuit"]["stopDispatch"])

    def test_brain_recovery_does_not_auto_resume(self) -> None:
        meeting_id = self._meeting("no auto brain resume")
        self._join_formal(meeting_id)
        self.app.start_meeting(meeting_id)
        self._fail_host(self.host_a)
        self.host_a.browser_connected = True
        self.host_a.page_alive = True
        self.host_a.composer_ready = True

        paused = self.app.snapshot(meeting_id)

        self.assertEqual(paused["meeting"]["status"], "PAUSED")
        self.assertTrue(paused["circuit"]["stopDispatch"])

def _request() -> BrainRequest:
    return BrainRequest(
        brain_request_id="phase3a-auth-test-request",
        meeting_id="phase3a-auth-test-meeting",
        task_id="phase3a-auth-test-task",
        prompt="read only",
        allowed_actions=("ACCEPT",),
    )


if __name__ == "__main__":
    unittest.main()
