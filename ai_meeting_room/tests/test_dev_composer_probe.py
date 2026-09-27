from __future__ import annotations

import json
import threading
import unittest
from urllib.error import HTTPError
import urllib.request

from ai_meeting_room.brain.playwright_attached_brain import (
    PlaywrightAttachedBrainHost,
    PlaywrightAttachedBrainState,
)
from ai_meeting_room.brain.playwright_brain import ChatGPTPlaywrightDomAdapter
from ai_meeting_room.product.server import UI_HTML, make_server


class ProbeLocator:
    def __init__(self, *, kind: str = "CONTENTEDITABLE", fill_error: Exception | None = None, value: str = "") -> None:
        self.kind = kind
        self.fill_error = fill_error
        self.value = value
        self.operations: list[tuple[str, str]] = []

    def count(self) -> int:
        return 1

    def nth(self, _index: int) -> "ProbeLocator":
        return self

    def is_visible(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True

    def is_editable(self) -> bool:
        return True

    def evaluate(self, _expression: str) -> dict[str, object]:
        return {
            "tagName": "DIV" if self.kind != "TEXTAREA" else "TEXTAREA",
            "role": "textbox",
            "contenteditable": self.kind in {"CONTENTEDITABLE", "PROSEMIRROR"},
            "dataTestId": "prompt-textarea",
            "ariaLabel": "Message",
            "placeholder": "Message",
            "classTokens": ["ProseMirror"] if self.kind == "PROSEMIRROR" else [],
        }

    def bounding_box(self) -> dict[str, int]:
        return {"x": 0, "y": 0, "width": 100, "height": 20}

    def input_value(self) -> str:
        if self.kind not in {"TEXTAREA", "INPUT"}:
            raise RuntimeError("not an input")
        return self.value

    def inner_text(self) -> str:
        return self.value

    def fill(self, value: str) -> None:
        self.operations.append(("fill", value))
        if self.fill_error is not None:
            raise self.fill_error
        self.value = value

    def focus(self) -> None:
        self.operations.append(("focus", ""))

    def press(self, key: str) -> None:
        self.operations.append(("press", key))
        if key in {"Enter", "Control+Enter", "Meta+Enter"}:
            raise AssertionError("probe must never submit")
        if key == "ControlOrMeta+A":
            self.value = ""

    def press_sequentially(self, value: str) -> None:
        self.operations.append(("press_sequentially", value))
        self.value = value


class ProbePage:
    def __init__(self, locator: ProbeLocator, *, user_turns: int = 3) -> None:
        self.url = "https://chatgpt.com/c/probe"
        self.locator_value = locator
        self.user_turns = user_turns
        self.locator_calls: list[str] = []
        self.send_calls = 0

    def locator(self, selector: str) -> ProbeLocator:
        self.locator_calls.append(selector)
        if "message-author-role=\"user\"" in selector:
            return _CountLocator(self.user_turns)
        if selector == ", ".join(ChatGPTPlaywrightDomAdapter.COMPOSER_SELECTORS):
            return self.locator_value
        if selector in ChatGPTPlaywrightDomAdapter.COMPOSER_SELECTORS:
            return self.locator_value
        return _CountLocator(0)

    def is_closed(self) -> bool:
        return False


class _CountLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    def count(self) -> int:
        return self._count

    def nth(self, _index: int) -> "_CountLocator":
        return self

    def is_visible(self) -> bool:
        return False

    def is_enabled(self) -> bool:
        return True

    def is_editable(self) -> bool:
        return False


class ProbeContext:
    def __init__(self, page: ProbePage) -> None:
        self.pages = [page]


class DevComposerProbeTests(unittest.TestCase):
    def make_host(self, locator: ProbeLocator) -> tuple[PlaywrightAttachedBrainHost, ProbePage]:
        page = ProbePage(locator)
        host = PlaywrightAttachedBrainHost.__new__(PlaywrightAttachedBrainHost)
        host.state = PlaywrightAttachedBrainState.READY
        host.auth_state = "AUTHENTICATED"
        host.browser = object()
        host.context = ProbeContext(page)
        host.page = page
        host.bound_page_id = "page-4"
        host.selected_candidate_index = 0
        host._browser_disconnected = False
        host.connect_attempt_id = None
        host.last_error = None
        host.last_error_details = None
        host._last_failure = None
        host.on_failure = None
        host.diagnostics = {"lastFailureStage": None}
        host.trace = []
        host._stage_started_at = {}
        host._stage_started_clock = {}
        host.attempt_started_at = None
        host.attempt_finished_at = None
        host.attempt_duration_ms = None
        host.owner_thread_id = threading.get_ident()
        host.runtime_owner = "PRODUCT_SHELL"
        host.registry_instance_id = "registry-test"
        host.host_instance_id = "host-test"
        host.process_pid = 1
        host.parent_pid = 0
        host._dev_probe_lock = threading.Lock()
        host._runtime_lifecycle_events = []
        host._runtime_lifecycle_counts = {
            "registryResetCount": 0,
            "hostReplacementCount": 0,
            "boundPageRebindCount": 0,
        }
        host._closing = False
        host._thread_operation_matrix = {}
        host.thread_affinity_error = None
        host._page_fingerprint = lambda _page, _index: {
            "candidateIndex": 0,
            "pathname": "/c/probe",
            "pageTitle": "ChatGPT",
            "isClosed": False,
            "visibilityState": "visible",
            "documentReadyState": "complete",
            "loginUiDetected": False,
            "authenticatedShellDetected": True,
            "composerCandidateCount": 1,
            "visibleComposerCount": 1,
            "editableComposerCount": 1,
        }
        return host, page

    def test_dev_probe_requires_ready_runtime(self) -> None:
        host, _ = self.make_host(ProbeLocator())
        host.state = PlaywrightAttachedBrainState.ERROR
        result = host.run_dev_composer_write_probe("probe", allow_send=False)
        self.assertEqual(result["failureCode"], "DEV_COMPOSER_PROBE_PRECONDITION_FAILED")

    def test_dev_probe_uses_current_bound_page(self) -> None:
        host, page = self.make_host(ProbeLocator(kind="TEXTAREA"))
        result = host.run_dev_composer_write_probe("probe", allow_send=False)
        self.assertIs(host.page, page)
        self.assertEqual(result["boundPageId"], "page-4")
        self.assertEqual(result["connectOverCdp"], "NOT_USED")

    def test_dev_probe_runs_all_steps_on_owner_thread(self) -> None:
        host, _ = self.make_host(ProbeLocator(kind="TEXTAREA"))
        result = host.run_dev_composer_write_probe("probe", allow_send=False)
        owner = result["ownerThreadId"]
        for name in ("resolve", "inspect", "write", "verify", "clear", "clear_verify"):
            self.assertEqual(result["threadIds"][name], owner)

    def test_dev_probe_send_is_forbidden(self) -> None:
        host, page = self.make_host(ProbeLocator(kind="TEXTAREA"))
        result = host.run_dev_composer_write_probe("probe", allow_send=False)
        self.assertFalse(result["allowSend"])
        self.assertEqual(page.send_calls, 0)

    def test_dev_probe_explicit_send_permission_is_rejected(self) -> None:
        host, page = self.make_host(ProbeLocator(kind="TEXTAREA"))
        result = host.run_dev_composer_write_probe("probe", allow_send=True)
        self.assertEqual(result["failureCode"], "DEV_PROBE_SEND_FORBIDDEN")
        self.assertEqual(result["probeState"], "FAILED")
        self.assertEqual(page.send_calls, 0)

    def test_dev_probe_never_presses_enter(self) -> None:
        locator = ProbeLocator(kind="CONTENTEDITABLE")
        host, _ = self.make_host(locator)
        host.run_dev_composer_write_probe("probe", allow_send=False)
        self.assertNotIn(("press", "Enter"), locator.operations)

    def test_dev_probe_records_element_kind(self) -> None:
        host, _ = self.make_host(ProbeLocator(kind="PROSEMIRROR"))
        result = host.run_dev_composer_write_probe("probe", allow_send=False)
        self.assertEqual(result["elementKind"], "PROSEMIRROR")
        self.assertTrue(result["domShape"]["boundingBoxExists"])

    def test_dev_probe_records_real_fill_error(self) -> None:
        locator = ProbeLocator(kind="PROSEMIRROR", fill_error=RuntimeError("fill unsupported"))
        host, _ = self.make_host(locator)
        result = host.run_dev_composer_write_probe("probe", allow_send=False)
        self.assertEqual(result["fillError"]["name"], "RuntimeError")
        self.assertEqual(result["fillError"]["message"], "fill unsupported")
        self.assertEqual(result["fillError"]["operation"], "locator.fill")
        self.assertEqual(result["fillError"]["elementKind"], "PROSEMIRROR")

    def test_dev_probe_write_verification(self) -> None:
        host, _ = self.make_host(ProbeLocator(kind="TEXTAREA"))
        result = host.run_dev_composer_write_probe("probe", allow_send=False)
        self.assertEqual(result["write"], "PASS")
        self.assertEqual(result["verify"], "PASS")

    def test_dev_probe_clear_verification(self) -> None:
        host, _ = self.make_host(ProbeLocator(kind="TEXTAREA"))
        result = host.run_dev_composer_write_probe("probe", allow_send=False)
        self.assertEqual(result["clear"], "PASS")
        self.assertEqual(result["clearVerify"], "PASS")

    def test_user_turn_count_unchanged(self) -> None:
        host, _ = self.make_host(ProbeLocator(kind="TEXTAREA"))
        result = host.run_dev_composer_write_probe("probe", allow_send=False)
        self.assertEqual(result["userTurnCountBefore"], result["userTurnCountAfter"])
        self.assertFalse(result["accidentalSend"])

    def test_probe_failure_does_not_change_auth_state(self) -> None:
        host, _ = self.make_host(ProbeLocator(kind="TEXTAREA", fill_error=RuntimeError("fill failed")))
        result = host.run_dev_composer_write_probe("probe", allow_send=False)
        self.assertEqual(result["authStateAfter"], "AUTHENTICATED")
        self.assertEqual(host.auth_state, "AUTHENTICATED")

    def test_probe_failure_does_not_clear_ready_state(self) -> None:
        host, _ = self.make_host(ProbeLocator(kind="TEXTAREA", fill_error=RuntimeError("fill failed")))
        result = host.run_dev_composer_write_probe("probe", allow_send=False)
        self.assertEqual(result["brainStateAfter"], PlaywrightAttachedBrainState.READY)
        self.assertEqual(host.state, PlaywrightAttachedBrainState.READY)

    def test_terminal_result_first_failure_is_preserved(self) -> None:
        locator = ProbeLocator(kind="TEXTAREA", fill_error=RuntimeError("fill failed"))
        host, _ = self.make_host(locator)
        result = host.run_dev_composer_write_probe("probe", allow_send=False)
        self.assertEqual(result["firstFailureCode"], "COMPOSER_WRITE_FAILED")
        self.assertNotEqual(result["firstFailureCode"], "AUTH_REQUIRED")

    def test_prosemirror_inner_editable_node_supported(self) -> None:
        locator = ProbeLocator(kind="PROSEMIRROR")
        host, _ = self.make_host(locator)
        result = host.run_dev_composer_write_probe("probe", allow_send=False)
        self.assertEqual(result["elementKind"], "PROSEMIRROR")
        self.assertEqual(result["strategy"], '[data-testid="prompt-textarea"]')

    def test_atomic_probe_uses_same_runtime_identity(self) -> None:
        host, _ = self.make_host(ProbeLocator(kind="TEXTAREA"))
        result = host.run_atomic_connect_and_composer_probe("probe", allow_send=False)
        self.assertTrue(result["atomicConnect"])
        self.assertTrue(result["sameRegistry"])
        self.assertTrue(result["sameHost"])
        self.assertTrue(result["sameBoundPage"])
        self.assertTrue(result["sameOwnerThread"])
        self.assertEqual(result["connectRegistryId"], result["probeRegistryId"])
        self.assertEqual(result["connectHostId"], result["probeHostId"])
        self.assertEqual(result["connectBoundPageId"], result["probeBoundPageId"])
        self.assertEqual(result["connectOwnerThreadId"], result["probeOwnerThreadId"])
        self.assertEqual(result["probeState"], "PASS")

    def test_atomic_probe_connect_and_write_same_registry(self) -> None:
        host, _ = self.make_host(ProbeLocator(kind="TEXTAREA"))
        result = host.run_atomic_connect_and_composer_probe("probe")
        self.assertTrue(result["sameRegistry"])

    def test_atomic_probe_connect_and_write_same_host(self) -> None:
        host, _ = self.make_host(ProbeLocator(kind="TEXTAREA"))
        result = host.run_atomic_connect_and_composer_probe("probe")
        self.assertTrue(result["sameHost"])

    def test_atomic_probe_connect_and_write_same_page(self) -> None:
        host, _ = self.make_host(ProbeLocator(kind="TEXTAREA"))
        result = host.run_atomic_connect_and_composer_probe("probe")
        self.assertTrue(result["sameBoundPage"])

    def test_atomic_probe_same_owner_thread(self) -> None:
        host, _ = self.make_host(ProbeLocator(kind="TEXTAREA"))
        result = host.run_atomic_connect_and_composer_probe("probe")
        self.assertTrue(result["sameOwnerThread"])

    def test_ready_runtime_not_destroyed_by_ui_polling(self) -> None:
        host, page = self.make_host(ProbeLocator(kind="TEXTAREA"))
        for _ in range(3):
            health = host.live_poc_health_check()
            self.assertEqual(health["precheckResult"], "PASS")
        self.assertIs(host.page, page)
        self.assertEqual(host.state, PlaywrightAttachedBrainState.READY)

    def test_ready_runtime_not_destroyed_by_poc_failure(self) -> None:
        host, page = self.make_host(ProbeLocator(kind="TEXTAREA", fill_error=RuntimeError("fill failed")))
        host.run_dev_composer_write_probe("probe")
        self.assertIs(host.page, page)
        self.assertEqual(host.state, PlaywrightAttachedBrainState.READY)

    def test_composer_failure_does_not_reset_host(self) -> None:
        host, page = self.make_host(ProbeLocator(kind="TEXTAREA", fill_error=RuntimeError("fill failed")))
        result = host.run_atomic_connect_and_composer_probe("probe")
        self.assertEqual(result["failureCode"], "COMPOSER_WRITE_FAILED")
        self.assertIs(host.page, page)
        self.assertEqual(host.state, PlaywrightAttachedBrainState.READY)

    def test_transient_health_failure_does_not_destroy_runtime(self) -> None:
        host, page = self.make_host(ProbeLocator(kind="TEXTAREA"))
        host._page_fingerprint = lambda _page, _index: {
            "candidateIndex": 0,
            "pathname": "/c/probe",
            "pageTitle": "ChatGPT",
            "isClosed": False,
            "visibilityState": "visible",
            "documentReadyState": "complete",
            "loginUiDetected": False,
            "authenticatedShellDetected": True,
            "composerCandidateCount": 0,
            "visibleComposerCount": 0,
            "editableComposerCount": 0,
        }
        result = host.run_atomic_connect_and_composer_probe("probe")
        self.assertEqual(result["failureCode"], "CHROME_RUNTIME_UNAVAILABLE")
        self.assertIs(host.page, page)
        self.assertEqual(host.state, PlaywrightAttachedBrainState.READY)

    def test_runtime_loss_never_silent(self) -> None:
        host, _ = self.make_host(ProbeLocator())
        host._on_browser_disconnect()
        event = host.lifecycle_snapshot()["lastEvent"]
        self.assertEqual(event["event"], "RUNTIME_LOSS_EVENT")
        self.assertEqual(event["reason"], "BROWSER_DISCONNECTED")

    def test_atomic_probe_preserves_ready_runtime_after_probe_failure(self) -> None:
        host, _ = self.make_host(ProbeLocator(kind="TEXTAREA", fill_error=RuntimeError("fill failed")))
        result = host.run_atomic_connect_and_composer_probe("probe", allow_send=False)
        self.assertEqual(result["probeState"], "FAILED")
        self.assertEqual(host.state, PlaywrightAttachedBrainState.READY)
        self.assertEqual(host.auth_state, "AUTHENTICATED")
        self.assertIsNotNone(host.page)

    def test_runtime_lifecycle_event_has_reason_and_caller(self) -> None:
        host, _ = self.make_host(ProbeLocator())
        host.close(reason_code="TEST_EXPLICIT_CLOSE", caller="test_runtime_lifecycle_event")
        event = host.lifecycle_snapshot()["lastEvent"]
        self.assertEqual(event["reason"], "TEST_EXPLICIT_CLOSE")
        self.assertEqual(event["caller"], "test_runtime_lifecycle_event")
        self.assertTrue(event["stackId"])

    def test_atomic_probe_send_kill_switch(self) -> None:
        host, page = self.make_host(ProbeLocator(kind="TEXTAREA"))
        result = host.run_atomic_connect_and_composer_probe("probe", allow_send=True)
        self.assertEqual(result["failureCode"], "DEV_PROBE_SEND_FORBIDDEN")
        self.assertFalse(result["atomicConnect"])
        self.assertEqual(page.send_calls, 0)


class DevComposerProbeRouteTests(unittest.TestCase):
    class _App:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def dev_composer_write_probe(self, body: dict[str, object]) -> dict[str, object]:
            self.calls.append(body)
            return {"ok": True, "probeState": "PASS", "allowSend": False}

        def dev_connect_and_composer_probe(self, body: dict[str, object]) -> dict[str, object]:
            self.calls.append(body)
            return {"ok": True, "probeState": "PASS", "atomicConnect": True}

    @staticmethod
    def _post(server: object, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        address = server.server_address
        url = f"http://{address[0]}:{address[1]}/api/dev/brain/composer-write-probe"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_dev_probe_route_is_opt_in_and_not_in_product_ui(self) -> None:
        self.assertNotIn("/api/dev/brain/composer-write-probe", UI_HTML)
        disabled = make_server(self._App(), host="127.0.0.1", port=0, dev_probe_enabled=False)
        enabled = make_server(self._App(), host="127.0.0.1", port=0, dev_probe_enabled=True)
        self.assertFalse(disabled.dev_probe_enabled)
        self.assertTrue(enabled.dev_probe_enabled)
        disabled.server_close()
        enabled.server_close()

    def test_dev_probe_route_dispatches_only_when_enabled(self) -> None:
        app = self._App()
        server = make_server(app, host="127.0.0.1", port=0, dev_probe_enabled=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, payload = self._post(server, {})
            self.assertEqual(status, 200)
            self.assertEqual(payload["probeState"], "PASS")
            self.assertEqual(app.calls, [{}])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_atomic_probe_route_dispatches_only_when_enabled(self) -> None:
        app = self._App()
        server = make_server(app, host="127.0.0.1", port=0, dev_probe_enabled=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            address = server.server_address
            url = f"http://{address[0]}:{address[1]}/api/dev/brain/connect-and-composer-probe"
            request = urllib.request.Request(
                url,
                data=json.dumps({}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertTrue(payload["atomicConnect"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
