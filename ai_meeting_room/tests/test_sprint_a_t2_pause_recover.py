from __future__ import annotations

import json
from io import BytesIO
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_meeting_room.core.meeting import MeetingStatus
from ai_meeting_room.persistence.sqlite_store import SQLiteStore
from ai_meeting_room.product.app import Phase2Application
from ai_meeting_room.product.server import ProductHandler
from test_phase2_product import FastCodexAdapter


class _HealthyResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _HandlerServerStub:
    dev_probe_enabled = False


class _CaptureBuffer(BytesIO):
    def close(self) -> None:
        # Keep the rendered HTTP response available after BaseRequestHandler.finish.
        self.flush()


class _MemoryRequest:
    def __init__(self, incoming: bytes) -> None:
        self.incoming = incoming
        self.output = _CaptureBuffer()

    def makefile(self, mode: str, _buffering: int = -1):
        if "r" in mode:
            return BytesIO(self.incoming)
        return self.output

    def sendall(self, data: bytes) -> None:
        self.output.write(data)

    def close(self) -> None:
        return None


class SprintAHttpPauseRecoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.adapters: list[FastCodexAdapter] = []

        def adapter_factory(**_kwargs):
            adapter = FastCodexAdapter()
            self.adapters.append(adapter)
            return adapter

        self.app = Phase2Application(SQLiteStore(root / "state.db"), adapter_factory=adapter_factory)
        self.meeting_id = self.app.create_meeting("Sprint A T2", str(root))["meeting"]["meeting_id"]
        self.agent_id = self.app.join_provider(self.meeting_id, "codex")["agents"][0]["agent_id"]
        core = self.app._core(self.meeting_id)
        core.mark_ready()
        core.start()

    def tearDown(self) -> None:
        self.app.close()
        self.temp.cleanup()

    @staticmethod
    def _handle_request(request: bytes, *, app, client_address: tuple[str, int]) -> tuple[int, dict]:
        handler_type = type("TestProductHandler", (ProductHandler,), {"app": app})
        transport = _MemoryRequest(request)
        handler_type(transport, client_address, _HandlerServerStub())
        headers, _, raw = transport.output.getvalue().partition(b"\r\n\r\n")
        status_code = int(headers.split(b" ", 2)[1])
        return status_code, json.loads(raw) if raw else {}

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        payload = b"{}" if body is None else json.dumps(body).encode("utf-8")
        request = (
            f"{method} {path} HTTP/1.1\r\n"
            "Host: localhost\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
        ).encode() + payload
        return self._handle_request(request, app=self.app, client_address=("127.0.0.1", 42000))

    def request_without_content_length(self, method: str, path: str) -> tuple[int, dict]:
        request = (
            f"{method} {path} HTTP/1.1\r\n"
            "Host: localhost\r\nConnection: close\r\n\r\n"
        ).encode()
        return self._handle_request(request, app=self.app, client_address=("127.0.0.1", 42000))

    def post_recover_with_healthy_cao(self) -> tuple[int, dict]:
        with patch("ai_meeting_room.product.app.urllib.request.urlopen", return_value=_HealthyResponse()):
            return self.request("POST", f"/api/meetings/{self.meeting_id}/recover")

    def test_01_running_pause_opens_breaker_and_persists_paused_state(self) -> None:
        status, body = self.request("POST", f"/api/meetings/{self.meeting_id}/pause", {"reason": "operator test"})

        self.assertEqual(status, 200)
        self.assertEqual(body["meeting"]["status"], "PAUSED")
        self.assertEqual(body["circuit"]["circuitState"], "OPEN")
        self.assertTrue(body["circuit"]["stopDispatch"])
        self.assertTrue(body["circuit"]["workspaceWriteProtected"])

    def test_02_repeated_pause_returns_typed_conflict_without_mutation(self) -> None:
        self.app.pause(self.meeting_id, "first pause")
        before_events = len(self.app._core(self.meeting_id).events.history())

        status, body = self.request("POST", f"/api/meetings/{self.meeting_id}/pause", {"reason": "second pause"})

        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "MEETING_NOT_RUNNING")
        self.assertEqual(len(self.app._core(self.meeting_id).events.history()), before_events)
        self.assertEqual(self.app.snapshot(self.meeting_id)["circuit"]["triggerReason"], "first pause")

    def test_03_pause_invalid_boundary_states_return_conflict(self) -> None:
        core = self.app._core(self.meeting_id)
        invalid_states = (
            MeetingStatus.CREATED,
            MeetingStatus.READY,
            MeetingStatus.PAUSED,
            MeetingStatus.RECOVERING,
            MeetingStatus.COMPLETED,
            MeetingStatus.FAILED,
        )
        for meeting_status in invalid_states:
            with self.subTest(status=meeting_status.value):
                core.meeting.meeting.status = meeting_status
                status, body = self.request("POST", f"/api/meetings/{self.meeting_id}/pause", {"reason": "must reject"})
                self.assertEqual(status, 409)
                self.assertEqual(body["error"]["code"], "MEETING_NOT_RUNNING")
                self.assertEqual(core.meeting.meeting.status, meeting_status)

    def test_04_recover_outside_paused_returns_typed_conflict(self) -> None:
        status, body = self.post_recover_with_healthy_cao()

        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "MEETING_NOT_PAUSED")
        self.assertEqual(self.app.snapshot(self.meeting_id)["meeting"]["status"], "RUNNING")
        self.assertEqual(self.app.snapshot(self.meeting_id)["circuit"]["circuitState"], "CLOSED")

    def test_05_unhealthy_agent_recovery_is_conflict_and_fails_closed(self) -> None:
        self.app.pause(self.meeting_id, "fault injection")
        self.adapters[0].raw_status = "stopped"

        status, body = self.post_recover_with_healthy_cao()

        snapshot = self.app.snapshot(self.meeting_id)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "RECOVERY_HEALTH_CHECK_FAILED")
        self.assertEqual(snapshot["meeting"]["status"], "PAUSED")
        self.assertEqual(snapshot["circuit"]["circuitState"], "OPEN")
        self.assertTrue(snapshot["circuit"]["stopDispatch"])
        self.assertTrue(snapshot["circuit"]["workspaceWriteProtected"])

    def test_06_component_health_failure_recovery_is_conflict_and_fails_closed(self) -> None:
        self.app.pause(self.meeting_id, "component failure")
        core = self.app._core(self.meeting_id)
        core.meeting.meeting.workspace_id = str(Path(self.temp.name) / "missing-workspace")
        with patch("ai_meeting_room.product.app.urllib.request.urlopen", return_value=_HealthyResponse()):
            status, body = self.request("POST", f"/api/meetings/{self.meeting_id}/recover")

        snapshot = self.app.snapshot(self.meeting_id)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "RECOVERY_HEALTH_CHECK_FAILED")
        self.assertEqual(snapshot["meeting"]["status"], "PAUSED")
        self.assertEqual(snapshot["circuit"]["circuitState"], "OPEN")
        self.assertTrue(snapshot["circuit"]["workspaceWriteProtected"])

    def test_07_local_only_gate_precedes_pause_and_recover_side_effects(self) -> None:
        class AppMustNotBeCalled:
            def pause(self, *_args, **_kwargs):
                raise AssertionError("remote pause reached application side effects")

            def recover(self, *_args, **_kwargs):
                raise AssertionError("remote recover reached application side effects")

        for route in ("pause", "recover"):
            with self.subTest(route=route):
                request = (
                    f"POST /api/meetings/meeting-1/{route} HTTP/1.1\r\n"
                    "Host: localhost\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
                ).encode()
                status, body = self._handle_request(request, app=AppMustNotBeCalled(), client_address=("10.0.0.5", 42000))
                self.assertEqual(status, 403)
                self.assertEqual(body["error"]["code"], "LOCAL_ONLY")

    def test_08_unknown_meeting_is_a_typed_client_error(self) -> None:
        status, body = self.request("POST", "/api/meetings/missing/pause", {"reason": "no side effect"})

        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "PRODUCT_REQUEST_FAILED")

    def test_09_healthy_recovery_returns_success_and_controlled_resume(self) -> None:
        self.app.pause(self.meeting_id, "resume test")

        status, body = self.post_recover_with_healthy_cao()

        self.assertEqual(status, 200)
        self.assertTrue(body["recovered"])
        self.assertEqual(body["meeting"]["status"], "RUNNING")
        self.assertEqual(body["circuit"]["circuitState"], "CLOSED")
        self.assertFalse(body["circuit"]["stopDispatch"])

    def test_10_recovery_safety_failure_returns_conflict_and_reopens_breaker(self) -> None:
        self.app.pause(self.meeting_id, "resume safety failure")
        core = self.app._core(self.meeting_id)
        with patch("ai_meeting_room.product.app.urllib.request.urlopen", return_value=_HealthyResponse()), patch.object(
            core.safety.breaker, "resume", side_effect=RuntimeError("injected safety resume failure")
        ):
            status, body = self.request("POST", f"/api/meetings/{self.meeting_id}/recover")

        snapshot = self.app.snapshot(self.meeting_id)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "RECOVERY_HEALTH_CHECK_FAILED")
        self.assertEqual(snapshot["meeting"]["status"], "PAUSED")
        self.assertEqual(snapshot["circuit"]["circuitState"], "OPEN")
        self.assertTrue(snapshot["circuit"]["stopDispatch"])

    def test_pr_02_pause_without_reason_uses_default_reason(self) -> None:
        status, body = self.request("POST", f"/api/meetings/{self.meeting_id}/pause", {})

        self.assertEqual(status, 200)
        self.assertEqual(body["meeting"]["pause_reason"], "operator requested pause")

    def test_pr_03_pause_with_no_content_length_accepts_empty_body(self) -> None:
        status, body = self.request_without_content_length("POST", f"/api/meetings/{self.meeting_id}/pause")

        self.assertEqual(status, 200)
        self.assertEqual(body["meeting"]["status"], "PAUSED")
        self.assertEqual(body["meeting"]["pause_reason"], "operator requested pause")

    def test_pr_04_pause_malformed_json_is_bad_request(self) -> None:
        payload = b"not-json"
        request = (
            f"POST /api/meetings/{self.meeting_id}/pause HTTP/1.1\r\n"
            f"Host: localhost\r\nContent-Length: {len(payload)}\r\n\r\n"
        ).encode() + payload

        status, body = self._handle_request(request, app=self.app, client_address=("127.0.0.1", 42000))

        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "PRODUCT_REQUEST_FAILED")
        self.assertEqual(self.app._core(self.meeting_id).meeting.meeting.status, MeetingStatus.RUNNING)

    def test_pr_05_pause_coerces_non_string_reason_to_string(self) -> None:
        status, body = self.request("POST", f"/api/meetings/{self.meeting_id}/pause", {"reason": 42})

        self.assertEqual(status, 200)
        self.assertEqual(body["meeting"]["pause_reason"], "42")

    def test_pr_13_get_pause_route_is_not_found(self) -> None:
        status, _body = self.request("GET", f"/api/meetings/{self.meeting_id}/pause")
        self.assertEqual(status, 404)

    def test_pr_14_pause_extra_path_segment_is_not_found(self) -> None:
        status, _body = self.request("POST", f"/api/meetings/{self.meeting_id}/pause/extra", {})
        self.assertEqual(status, 404)

    def test_pr_15_double_slash_path_is_normalized(self) -> None:
        status, body = self.request("POST", f"//api/meetings/{self.meeting_id}/pause", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["meeting"]["status"], "PAUSED")

    def test_pr_16_pause_does_not_close_or_replace_brain(self) -> None:
        brain = self.app._brains[self.meeting_id]
        status, _body = self.request("POST", f"/api/meetings/{self.meeting_id}/pause", {"reason": "manual"})

        self.assertEqual(status, 200)
        self.assertIs(self.app._brains[self.meeting_id], brain)
        self.assertTrue(brain.health_check())

    def test_pr_17_concurrent_pause_and_recover_are_serialized(self) -> None:
        barrier = threading.Barrier(3)
        outcomes: list[str] = []

        def pause():
            barrier.wait()
            try:
                self.app.pause(self.meeting_id, "concurrent pause")
                outcomes.append("PAUSE_OK")
            except Exception as exc:
                outcomes.append(f"PAUSE_{type(exc).__name__}")

        def recover():
            barrier.wait()
            try:
                with patch("ai_meeting_room.product.app.urllib.request.urlopen", return_value=_HealthyResponse()):
                    result = self.app.recover(self.meeting_id)
                outcomes.append("RECOVER_OK" if result["recovered"] else "RECOVER_BLOCKED")
            except Exception as exc:
                outcomes.append(f"RECOVER_{type(exc).__name__}")

        threads = [threading.Thread(target=pause), threading.Thread(target=recover)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=3)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(outcomes), 2)
        snapshot = self.app.snapshot(self.meeting_id)
        if snapshot["meeting"]["status"] == "PAUSED":
            self.assertEqual(snapshot["circuit"]["circuitState"], "OPEN")
        else:
            self.assertEqual(snapshot["meeting"]["status"], "RUNNING")
            self.assertEqual(snapshot["circuit"]["circuitState"], "CLOSED")

    def test_pr_19_pause_state_and_breaker_are_persisted(self) -> None:
        self.app.pause(self.meeting_id, "persisted pause")

        from ai_meeting_room.core.service import MeetingCore

        restored = MeetingCore.restore(self.meeting_id, self.app.store)
        self.assertEqual(restored.meeting.meeting.status, MeetingStatus.PAUSED)
        self.assertEqual(restored.safety.breaker.state.value, "OPEN")
        self.assertTrue(restored.safety.breaker.workspace_write_protected)

    def test_rr_04_05_06_recover_requires_paused_from_all_other_boundary_states(self) -> None:
        core = self.app._core(self.meeting_id)
        for meeting_status in (
            MeetingStatus.CREATED,
            MeetingStatus.READY,
            MeetingStatus.COMPLETED,
            MeetingStatus.FAILED,
        ):
            with self.subTest(status=meeting_status.value):
                core.meeting.meeting.status = meeting_status
                status, body = self.post_recover_with_healthy_cao()
                self.assertEqual(status, 409)
                self.assertEqual(body["error"]["code"], "MEETING_NOT_PAUSED")
                self.assertEqual(core.meeting.meeting.status, meeting_status)

    def test_rr_08_cao_unavailable_recovery_is_conflict_and_fails_closed(self) -> None:
        self.app.pause(self.meeting_id, "CAO unavailable")
        with patch("ai_meeting_room.product.app.urllib.request.urlopen", side_effect=OSError("CAO offline")):
            status, body = self.request("POST", f"/api/meetings/{self.meeting_id}/recover")

        snapshot = self.app.snapshot(self.meeting_id)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "RECOVERY_HEALTH_CHECK_FAILED")
        self.assertEqual(snapshot["meeting"]["status"], "PAUSED")
        self.assertEqual(snapshot["circuit"]["circuitState"], "OPEN")
        self.assertTrue(snapshot["circuit"]["stopDispatch"])

    def test_rr_10_recovery_without_registered_brain_is_rejected(self) -> None:
        self.app.pause(self.meeting_id, "brain missing")
        brain = self.app._brains.pop(self.meeting_id)
        try:
            status, body = self.post_recover_with_healthy_cao()
        finally:
            self.app._brains[self.meeting_id] = brain

        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "RECOVERY_HEALTH_CHECK_FAILED")
        self.assertEqual(self.app._core(self.meeting_id).meeting.meeting.status, MeetingStatus.PAUSED)
        self.assertEqual(self.app._core(self.meeting_id).safety.breaker.state.value, "OPEN")

    def test_rr_13_unknown_meeting_recover_is_bad_request(self) -> None:
        status, body = self.request("POST", "/api/meetings/missing/recover")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "PRODUCT_REQUEST_FAILED")

    def test_rr_14_recover_ignores_optional_request_fields(self) -> None:
        self.app.pause(self.meeting_id, "recover request body")
        with patch("ai_meeting_room.product.app.urllib.request.urlopen", return_value=_HealthyResponse()):
            status, body = self.request("POST", f"/api/meetings/{self.meeting_id}/recover", {"reason": "ignored"})
        self.assertEqual(status, 200)
        self.assertTrue(body["recovered"])

    def test_rr_15_recover_with_no_content_length_succeeds(self) -> None:
        self.app.pause(self.meeting_id, "empty recover request")
        with patch("ai_meeting_room.product.app.urllib.request.urlopen", return_value=_HealthyResponse()):
            status, body = self.request_without_content_length("POST", f"/api/meetings/{self.meeting_id}/recover")
        self.assertEqual(status, 200)
        self.assertTrue(body["recovered"])

    def test_rr_16_get_recover_route_is_not_found(self) -> None:
        status, _body = self.request("GET", f"/api/meetings/{self.meeting_id}/recover")
        self.assertEqual(status, 404)

    def test_rr_17_pause_then_recover_completes_controlled_lifecycle(self) -> None:
        pause_status, pause_body = self.request("POST", f"/api/meetings/{self.meeting_id}/pause", {"reason": "round trip"})
        recover_status, recover_body = self.post_recover_with_healthy_cao()

        self.assertEqual(pause_status, 200)
        self.assertEqual(pause_body["meeting"]["status"], "PAUSED")
        self.assertEqual(recover_status, 200)
        self.assertEqual(recover_body["meeting"]["status"], "RUNNING")
        self.assertEqual(recover_body["circuit"]["circuitState"], "CLOSED")

    def test_rr_19_recovered_checkpoint_persists_but_restore_still_fails_closed(self) -> None:
        self.app.pause(self.meeting_id, "persisted recovery")
        status, body = self.post_recover_with_healthy_cao()

        self.assertEqual(status, 200)
        self.assertTrue(body["recovered"])
        persisted_meeting = self.app.store.get_meeting(self.meeting_id)
        persisted_circuit = self.app.store.get_circuit(self.meeting_id)
        self.assertEqual(persisted_meeting["status"], "RUNNING")
        self.assertEqual(persisted_circuit["circuitState"], "CLOSED")

        # Safety invariant: a new process must not resume dispatch from persisted RUNNING.
        from ai_meeting_room.core.service import MeetingCore

        restored = MeetingCore.restore(self.meeting_id, self.app.store)
        self.assertEqual(restored.meeting.meeting.status, MeetingStatus.PAUSED)
        self.assertEqual(restored.safety.breaker.state.value, "OPEN")
        self.assertTrue(restored.safety.breaker.stop_dispatch)


if __name__ == "__main__":
    unittest.main()
