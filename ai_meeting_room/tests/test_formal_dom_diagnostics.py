from __future__ import annotations

import json
import io
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_meeting_room.brain.dedicated_cdp import DevToolsActivePortRecord
from ai_meeting_room.brain.playwright_attached_brain import (
    PlaywrightAttachedBrainHost,
    _STRUCTURAL_DIAGNOSTIC_SCRIPT,
)
from ai_meeting_room.brain.runtime_registry import FormalBrainRuntimeRegistry, FormalBrainRuntimeService
from ai_meeting_room.product.server import ProductHandler


def _probe_rows(probe_id: str, *, count: int = 0, visible: int = 0, editable: int = 0) -> list[dict[str, object]]:
    return [{"probe_id": probe_id, "count": count, "visible_count": visible, "editable_count": editable}]


def _frame_result(*, composer_count: int = 0, composer_visible: int = 0, composer_editable: int = 0) -> dict[str, object]:
    return {
        "document_ready_state": "complete",
        "body_present": True,
        "main_element_count": 1,
        "form_count": 1,
        "textarea_count": 1,
        "contenteditable_true_count": 0,
        "role_textbox_count": 1,
        "iframe_count": 0,
        "dialog_count": 0,
        "button_count": 2,
        "visible_textarea_count": 1,
        "visible_contenteditable_count": 0,
        "visible_role_textbox_count": 1,
        "visible_editable_form_control_count": 1,
        "form_with_editable_descendant_count": 1,
        "probe_results": {
            "APP_SHELL_PROBES": _probe_rows("main", count=1, visible=1),
            "COMPOSER_PROBES": _probe_rows(
                "prompt-testid", count=composer_count, visible=composer_visible, editable=composer_editable
            ),
            "AUTHENTICATED_PROBES": _probe_rows("main", count=1, visible=1),
            "LOGIN_PROBES": _probe_rows("email-input"),
            "CHALLENGE_PROBES": _probe_rows("challenge-testid"),
            "ERROR_PROBES": _probe_rows("role-alert"),
        },
    }


class _Frame:
    def __init__(self, url: str, result: dict[str, object] | None = None) -> None:
        self.url = url
        self.result = result or _frame_result()
        self.evaluate_calls = 0
        self.last_script: str | None = None

    def evaluate(self, script: str) -> dict[str, object]:
        self.evaluate_calls += 1
        self.last_script = script
        return self.result


class _Page:
    def __init__(self, url: str = "https://chatgpt.com/", *, frame_result=None) -> None:
        self.url = url
        self.closed = False
        self.main_frame = _Frame(url, frame_result or _frame_result())
        self.frames = [self.main_frame]

    def is_closed(self) -> bool:
        return self.closed


class _Context:
    def __init__(self, pages: list[_Page]) -> None:
        self.pages = pages


class _Browser:
    def __init__(self, contexts: list[_Context]) -> None:
        self.contexts = contexts
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected


def _make_formal_service(*, frame_result=None):
    page = _Page(frame_result=frame_result)
    context = _Context([page])
    browser = _Browser([context])
    factory_calls = 0

    def playwright_factory():
        nonlocal factory_calls
        factory_calls += 1
        return object()

    record = DevToolsActivePortRecord(
        path=Path("/test/DevToolsActivePort"),
        port=43123,
        ws_path="/devtools/browser/structural-diagnostic-test",
    )
    with patch(
        "ai_meeting_room.brain.playwright_attached_brain.chromium_channel_support",
        return_value={"supported": True},
    ):
        host = PlaywrightAttachedBrainHost(
            playwright_factory=playwright_factory,
            endpoint_resolver=lambda: record,
            attach_mode="EXACT_DEDICATED_CDP",
            endpoint_source="DEVTOOLS_ACTIVE_PORT",
        )
    host.browser = browser
    host.playwright = object()
    host.context = context
    host.page = page
    host._bound_page_object = page
    host.bound_page_id = "page-0"
    host.state = "ERROR"
    host.auth_state = "AUTHENTICATED"
    host._capture_diagnostic_binding_baseline(page, context)
    registry = FormalBrainRuntimeRegistry(host)
    service = FormalBrainRuntimeService(registry)
    return service, host, registry, browser, page, context, lambda: factory_calls


class FormalDomDiagnosticHostTests(unittest.TestCase):
    def test_diagnostic_schema_is_fixed(self) -> None:
        service, *_ = _make_formal_service()

        result = service.get_chatgpt_structural_diagnostics()

        self.assertEqual(
            set(result),
            {
                "DIAGNOSTIC_SCHEMA_VERSION",
                "PAGE_INSTANCE_FINGERPRINT",
                "MAIN_FRAME_FINGERPRINT",
                "BROWSER_CONTEXT_FINGERPRINT",
                "PAGE_URL_HOST_CLASS",
                "DOCUMENT_READY_STATE",
                "MAIN_FRAME_HOST_CLASS",
                "FRAME_COUNT",
                "FRAME_HOST_CLASSES",
                "BODY_PRESENT",
                "MAIN_ELEMENT_COUNT",
                "FORM_COUNT",
                "TEXTAREA_COUNT",
                "CONTENTEDITABLE_TRUE_COUNT",
                "ROLE_TEXTBOX_COUNT",
                "IFRAME_COUNT",
                "DIALOG_COUNT",
                "BUTTON_COUNT",
                "VISIBLE_TEXTAREA_COUNT",
                "VISIBLE_CONTENTEDITABLE_COUNT",
                "VISIBLE_ROLE_TEXTBOX_COUNT",
                "VISIBLE_EDITABLE_FORM_CONTROL_COUNT",
                "FORM_WITH_EDITABLE_DESCENDANT_COUNT",
                "APP_SHELL_PROBES",
                "COMPOSER_PROBES",
                "AUTHENTICATED_PROBES",
                "LOGIN_PROBES",
                "CHALLENGE_PROBES",
                "ERROR_PROBES",
                "COMPOSER_FRAME_EVIDENCE",
                "CURRENT_PAGE_STILL_IN_CONTEXT",
                "BOUND_PAGE_IS_CLOSED",
                "PAGE_INSTANCE_CHANGED_SINCE_BIND",
                "MAIN_FRAME_CHANGED_SINCE_BIND",
                "CONTEXT_CHANGED_SINCE_BIND",
                "PAGE_COUNT_CURRENT",
                "CONTEXT_COUNT_CURRENT",
                "AUTH_STATE",
                "COMPOSER_READY",
                "BRAIN_STATE",
                "COMPOSER_ROOT_CAUSE_CLASS",
                "ROOT_CAUSE_CONFIDENCE",
                "ROOT_CAUSE_EVIDENCE",
            },
        )

    def test_diagnostic_rejects_arbitrary_selector(self) -> None:
        service, *_ = _make_formal_service()

        with self.assertRaises(TypeError):
            service.get_chatgpt_structural_diagnostics(selector="body")
        with self.assertRaises(TypeError):
            service.get_chatgpt_structural_diagnostics(script="() => document.body")
        self.assertNotIn("selector", inspect.signature(service.get_chatgpt_structural_diagnostics).parameters)
        self.assertNotIn("script", inspect.signature(service.get_chatgpt_structural_diagnostics).parameters)

    def test_diagnostic_returns_no_text_content(self) -> None:
        service, _host, _registry, _browser, page, _context, _factory_calls = _make_formal_service()

        result = service.get_chatgpt_structural_diagnostics()

        self.assertNotIn("innerText", _STRUCTURAL_DIAGNOSTIC_SCRIPT)
        self.assertNotIn("textContent", _STRUCTURAL_DIAGNOSTIC_SCRIPT)
        self.assertIs(page.main_frame.last_script, _STRUCTURAL_DIAGNOSTIC_SCRIPT)
        self.assertNotIn("fixture-private-message", json.dumps(result))
        self.assertFalse(any(key.lower() in {"text", "innertext", "textcontent", "html", "message", "conversationtitle"}
                             for key in _all_keys(result)))

    def test_diagnostic_returns_no_input_values(self) -> None:
        service, _host, _registry, _browser, page, _context, _factory_calls = _make_formal_service()

        service.get_chatgpt_structural_diagnostics()

        forbidden_script_fragments = (
            "textContent",
            "innerText",
            ".value",
        )
        for fragment in forbidden_script_fragments:
            self.assertNotIn(fragment, _STRUCTURAL_DIAGNOSTIC_SCRIPT)
        self.assertEqual(page.main_frame.evaluate_calls, 1)

    def test_diagnostic_returns_no_storage_or_cookie_data(self) -> None:
        service, *_ = _make_formal_service()

        result = service.get_chatgpt_structural_diagnostics()

        for fragment in ("document.cookie", "localStorage", "sessionStorage", "innerHTML", "outerHTML"):
            self.assertNotIn(fragment, _STRUCTURAL_DIAGNOSTIC_SCRIPT)
        response_keys = {key.lower() for key in _all_keys(result)}
        self.assertFalse(any(any(term in key for term in ("cookie", "token", "storage", "html")) for key in response_keys))

    def test_diagnostic_reports_current_page_identity(self) -> None:
        service, _host, _registry, _browser, _page, _context, _factory_calls = _make_formal_service()

        result = service.get_chatgpt_structural_diagnostics()

        self.assertRegex(result["PAGE_INSTANCE_FINGERPRINT"], r"^[0-9a-f]{24}$")
        self.assertEqual(result["PAGE_URL_HOST_CLASS"], "CHATGPT")

    def test_diagnostic_reports_main_frame_identity(self) -> None:
        service, *_ = _make_formal_service()

        result = service.get_chatgpt_structural_diagnostics()

        self.assertRegex(result["MAIN_FRAME_FINGERPRINT"], r"^[0-9a-f]{24}$")
        self.assertEqual(result["MAIN_FRAME_HOST_CLASS"], "CHATGPT")

    def test_diagnostic_reports_context_identity(self) -> None:
        service, *_ = _make_formal_service()

        result = service.get_chatgpt_structural_diagnostics()

        self.assertRegex(result["BROWSER_CONTEXT_FINGERPRINT"], r"^[0-9a-f]{24}$")

    def test_diagnostic_reports_structural_counts(self) -> None:
        service, *_ = _make_formal_service(frame_result=_frame_result(composer_count=1, composer_visible=1, composer_editable=1))

        result = service.get_chatgpt_structural_diagnostics()

        self.assertEqual(result["FRAME_COUNT"], 1)
        self.assertTrue(result["BODY_PRESENT"])
        self.assertEqual(result["MAIN_ELEMENT_COUNT"], 1)
        self.assertEqual(result["FORM_COUNT"], 1)
        self.assertEqual(result["TEXTAREA_COUNT"], 1)
        self.assertEqual(result["VISIBLE_EDITABLE_FORM_CONTROL_COUNT"], 1)

    def test_diagnostic_reports_fixed_probe_counts(self) -> None:
        service, *_ = _make_formal_service(frame_result=_frame_result(composer_count=1, composer_visible=1, composer_editable=1))

        result = service.get_chatgpt_structural_diagnostics()

        for category in (
            "APP_SHELL_PROBES",
            "COMPOSER_PROBES",
            "AUTHENTICATED_PROBES",
            "LOGIN_PROBES",
            "CHALLENGE_PROBES",
            "ERROR_PROBES",
        ):
            self.assertTrue(result[category])
            self.assertEqual(set(result[category][0]), {"probe_id", "present", "count", "visible_count", "editable_count"})
        self.assertEqual(result["COMPOSER_PROBES"][0]["editable_count"], 1)

    def test_diagnostic_reports_visible_editable_counts(self) -> None:
        service, *_ = _make_formal_service()

        result = service.get_chatgpt_structural_diagnostics()

        self.assertEqual(result["VISIBLE_TEXTAREA_COUNT"], 1)
        self.assertEqual(result["VISIBLE_ROLE_TEXTBOX_COUNT"], 1)
        self.assertEqual(result["FORM_WITH_EDITABLE_DESCENDANT_COUNT"], 1)

    def test_diagnostic_detects_page_closed(self) -> None:
        service, _host, _registry, _browser, page, _context, _factory_calls = _make_formal_service()
        page.closed = True
        closed = service.get_chatgpt_structural_diagnostics()
        self.assertTrue(closed["BOUND_PAGE_IS_CLOSED"])

    def test_diagnostic_detects_page_replacement(self) -> None:
        service, host, _registry, _browser, _page, _context, _factory_calls = _make_formal_service()
        replacement = _Page("https://chatgpt.com/")
        host.page = replacement
        host.context.pages.append(replacement)
        changed = service.get_chatgpt_structural_diagnostics()
        self.assertTrue(changed["PAGE_INSTANCE_CHANGED_SINCE_BIND"])
        self.assertTrue(changed["MAIN_FRAME_CHANGED_SINCE_BIND"])

    def test_diagnostic_preserves_authenticated_state(self) -> None:
        service, host, *_ = _make_formal_service()
        before = host.auth_state

        result = service.get_chatgpt_structural_diagnostics()

        self.assertEqual(host.auth_state, before)
        self.assertEqual(result["AUTH_STATE"], "AUTHENTICATED")

    def test_diagnostic_has_no_side_effect(self) -> None:
        service, host, _registry, browser, page, context, factory_calls = _make_formal_service()
        before = (host.auth_state, host.state, host.page, host.context, tuple(context.pages), browser.connected)

        result = service.get_chatgpt_structural_diagnostics()

        after = (host.auth_state, host.state, host.page, host.context, tuple(context.pages), browser.connected)
        self.assertEqual(before, after)
        self.assertEqual(result["AUTH_STATE"], "AUTHENTICATED")
        self.assertEqual(result["BRAIN_STATE"], "ERROR")
        self.assertFalse(result["COMPOSER_READY"])
        self.assertEqual(factory_calls(), 0)
        self.assertEqual(page.main_frame.evaluate_calls, 1)

    def test_diagnostic_runs_on_owner_thread(self) -> None:
        service, host, _registry, _browser, _page, _context, factory_calls = _make_formal_service()

        service.get_chatgpt_structural_diagnostics()

        entry = host._thread_operation_matrix["get_chatgpt_structural_diagnostics"]
        self.assertEqual(entry["callerThreadId"], host.owner_thread_id)
        self.assertEqual(entry["playwrightThreadId"], host.owner_thread_id)
        self.assertEqual(factory_calls(), 0)
        self.assertEqual(host.diagnostics["connectOverCdp"], "NOT_RUN")

    def test_diagnostic_detects_page_outside_context(self) -> None:
        service, host, _registry, _browser, page, context, _factory_calls = _make_formal_service()
        context.pages.clear()
        result = service.get_chatgpt_structural_diagnostics()
        self.assertFalse(result["CURRENT_PAGE_STILL_IN_CONTEXT"])

    def test_diagnostic_detects_context_replacement(self) -> None:
        service, host, _registry, _browser, page, _context, _factory_calls = _make_formal_service()
        replacement_context = _Context([page])
        host.context = replacement_context
        host.browser.contexts.append(replacement_context)
        result = service.get_chatgpt_structural_diagnostics()
        self.assertTrue(result["CONTEXT_CHANGED_SINCE_BIND"])

    def test_diagnostic_does_not_create_runtime(self) -> None:
        service, _host, _registry, _browser, _page, _context, factory_calls = _make_formal_service()

        service.get_chatgpt_structural_diagnostics()

        self.assertEqual(factory_calls(), 0)

    def test_diagnostic_does_not_reconnect_browser(self) -> None:
        service, host, *_ = _make_formal_service()
        with patch.object(host, "connect", side_effect=AssertionError("diagnostic attempted reconnect")):
            service.get_chatgpt_structural_diagnostics()
        self.assertEqual(host.diagnostics["connectOverCdp"], "NOT_RUN")

    def test_diagnostic_detects_composer_in_different_frame(self) -> None:
        service, _host, _registry, _browser, page, _context, _factory_calls = _make_formal_service()
        page.frames.append(_Frame(
            "https://auth.openai.com/",
            _frame_result(composer_count=1, composer_visible=1, composer_editable=1),
        ))

        result = service.get_chatgpt_structural_diagnostics()

        self.assertEqual(result["FRAME_COUNT"], 2)
        self.assertEqual(result["FRAME_HOST_CLASSES"], ["CHATGPT", "OPENAI"])
        self.assertEqual(result["COMPOSER_ROOT_CAUSE_CLASS"], "COMPOSER_IN_DIFFERENT_FRAME")

    def test_diagnostic_identifies_semantic_composer_selector_drift(self) -> None:
        service, *_ = _make_formal_service(frame_result=_frame_result())

        result = service.get_chatgpt_structural_diagnostics()

        self.assertGreater(result["VISIBLE_EDITABLE_FORM_CONTROL_COUNT"], 0)
        self.assertEqual(result["COMPOSER_ROOT_CAUSE_CLASS"], "COMPOSER_SELECTOR_DRIFT")


def _all_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _all_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_keys(child)


class FormalDomDiagnosticRouteTests(unittest.TestCase):
    @staticmethod
    def _run_route(body: bytes, app, address=("127.0.0.1", 12345)):
        class _Handler(ProductHandler):
            def __init__(self) -> None:
                self.path = "/api/brain/real-chrome/structural-diagnostics"
                self.client_address = address
                self.rfile = io.BytesIO(body)
                self.headers = {"Content-Length": str(len(body))}
                self.app = app
                self.server = SimpleNamespace()
                self.response: tuple[int, object] | None = None

            def _json(self, status: int, payload: object) -> None:
                self.response = (status, payload)

        handler = _Handler()
        handler.do_POST()
        return handler.response

    def test_route_rejects_caller_selector_or_script(self) -> None:
        class _App:
            def get_chatgpt_structural_diagnostics(self):
                raise AssertionError("diagnostic must not run for caller options")

        response = self._run_route(json.dumps({"selector": "body", "script": "() => document.body"}).encode(), _App())

        self.assertIsNotNone(response)
        self.assertEqual(response[0], 400)
        self.assertEqual(response[1]["error"]["code"], "UNEXPECTED_STRUCTURAL_DIAGNOSTIC_OPTIONS")

    def test_route_accepts_empty_body_and_delegates_once(self) -> None:
        class _App:
            calls = 0

            def get_chatgpt_structural_diagnostics(self):
                self.calls += 1
                return {"DIAGNOSTIC_SCHEMA_VERSION": "P3A_STRUCTURAL_V1"}

        app = _App()
        response = self._run_route(b"", app)

        self.assertEqual(response, (200, {"DIAGNOSTIC_SCHEMA_VERSION": "P3A_STRUCTURAL_V1"}))
        self.assertEqual(app.calls, 1)

    def test_route_is_loopback_only(self) -> None:
        class _App:
            def get_chatgpt_structural_diagnostics(self):
                raise AssertionError("remote caller must not reach diagnostic")

        response = self._run_route(b"", _App(), address=("192.0.2.5", 12345))

        self.assertIsNotNone(response)
        self.assertEqual(response[0], 403)
        self.assertEqual(response[1]["error"]["code"], "LOCAL_ONLY")


if __name__ == "__main__":
    unittest.main()
