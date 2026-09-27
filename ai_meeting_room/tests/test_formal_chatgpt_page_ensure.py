from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from ai_meeting_room.brain.dedicated_cdp import DevToolsActivePortRecord
from ai_meeting_room.brain.playwright_attached_brain import PlaywrightAttachedBrainError, PlaywrightAttachedBrainHost
from ai_meeting_room.brain.runtime_registry import FormalBrainRuntimeRegistry, FormalBrainRuntimeService
from ai_meeting_room.product.server import make_server


class _Page:
    def __init__(self, url: str = "about:blank") -> None:
        self.url = url
        self.operations: list[str] = []
        self.handlers: list[str] = []
        self.closed = False
        self.goto_thread_id: int | None = None

    def on(self, event: str, _callback) -> None:
        self.handlers.append(event)

    def is_closed(self) -> bool:
        return self.closed

    def evaluate(self, _script):
        self.operations.append("evaluate_fingerprint")
        return {
            "visibilityState": "visible",
            "documentReadyState": "complete",
            "loginUiDetected": False,
            "authenticatedShellDetected": True,
            "composerCandidateCount": 1,
            "visibleComposerCount": 1,
            "editableComposerCount": 1,
        }

    def goto(self, url: str, *, wait_until: str) -> None:
        self.operations.append("goto")
        self.goto_thread_id = threading.get_ident()
        self.url = url
        self.wait_until = wait_until

    def bring_to_front(self) -> None:
        self.operations.append("bring_to_front")


class _Context:
    def __init__(self, pages=()) -> None:
        self.pages = list(pages)
        self.created_pages: list[_Page] = []
        self.created_thread_ids: list[int] = []
        self.on_new_page = None

    def new_page(self) -> _Page:
        page = _Page()
        self.pages.append(page)
        self.created_pages.append(page)
        self.created_thread_ids.append(threading.get_ident())
        if self.on_new_page is not None:
            self.on_new_page()
        return page


class _Browser:
    def __init__(self, contexts) -> None:
        self.contexts = list(contexts)
        self.connected = True

    def on(self, *_args) -> None:
        pass

    def is_connected(self) -> bool:
        return self.connected


class _Chromium:
    def __init__(self, browser: _Browser) -> None:
        self.browser = browser
        self.connect_calls = 0

    def connect_over_cdp(self, _endpoint: str, **_kwargs) -> _Browser:
        self.connect_calls += 1
        return self.browser


class _Playwright:
    def __init__(self, chromium: _Chromium) -> None:
        self.chromium = chromium
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


def _make_service(*contexts: _Context, fail_factory_on_repeat: bool = False):
    browser = _Browser(contexts)
    chromium = _Chromium(browser)
    runtime = _Playwright(chromium)
    factory_calls = 0

    def runtime_factory():
        nonlocal factory_calls
        factory_calls += 1
        if fail_factory_on_repeat and factory_calls > 1:
            raise RuntimeError("It looks like you are using Playwright Sync API inside the asyncio loop")
        return runtime

    record = DevToolsActivePortRecord(
        path=Path("/test/DevToolsActivePort"),
        port=43123,
        ws_path="/devtools/browser/formal-page-ensure-test",
    )
    with patch(
        "ai_meeting_room.brain.playwright_attached_brain.chromium_channel_support",
        return_value={"supported": True},
    ):
        host = PlaywrightAttachedBrainHost(
            playwright_factory=runtime_factory,
            endpoint_resolver=lambda: record,
            attach_mode="EXACT_DEDICATED_CDP",
            endpoint_source="DEVTOOLS_ACTIVE_PORT",
        )
    registry = FormalBrainRuntimeRegistry(host, runtime_owner="PRODUCT_SHELL")
    service = FormalBrainRuntimeService(registry)
    return service, host, registry, browser, chromium, runtime, lambda: factory_calls


class FormalChatGPTPageEnsureTests(unittest.TestCase):
    def test_ensure_chatgpt_page_reuses_existing_page(self) -> None:
        page = _Page("https://chatgpt.com/")
        context = _Context([page])
        service, host, _registry, browser, chromium, _runtime, factory_calls = _make_service(context)
        service.connect(connect_attempt_id="ensure-existing-page")
        foreground_count_before = page.operations.count("bring_to_front")

        result = service.ensure_chatgpt_page()

        self.assertEqual(result["action"], "REUSED_EXISTING_PAGE")
        self.assertIs(host.page, page)
        self.assertIs(host.context, context)
        self.assertIs(host.browser, browser)
        self.assertEqual(len(context.pages), 1)
        self.assertEqual(page.handlers.count("close"), 1)
        self.assertEqual(page.handlers.count("framenavigated"), 1)
        self.assertEqual(page.operations.count("bring_to_front"), foreground_count_before + 1)
        self.assertEqual(chromium.connect_calls, 1)
        self.assertEqual(factory_calls(), 1)

    def test_ensure_chatgpt_page_creates_page_when_missing(self) -> None:
        context = _Context()
        service, host, _registry, browser, chromium, _runtime, factory_calls = _make_service(context)
        with self.assertRaises(PlaywrightAttachedBrainError) as connect_error:
            service.connect(connect_attempt_id="ensure-missing-page")
        self.assertEqual(connect_error.exception.code, "CHATGPT_PAGE_NOT_FOUND")
        self.assertIs(host.browser, browser)

        result = service.ensure_chatgpt_page()

        self.assertEqual(result["action"], "CREATED_NEW_PAGE")
        self.assertEqual(result["selectedTargetHostname"], "chatgpt.com")
        self.assertEqual(result["chatgptPageCount"], 1)
        self.assertEqual(len(context.created_pages), 1)
        self.assertEqual(context.created_pages[0].url, "https://chatgpt.com/")
        self.assertEqual(context.created_pages[0].wait_until, "domcontentloaded")
        self.assertEqual(context.created_pages[0].operations.count("bring_to_front"), 1)
        self.assertIs(host.context, context)
        self.assertIs(host.browser, browser)
        self.assertEqual(chromium.connect_calls, 1)
        self.assertEqual(factory_calls(), 1)

    def test_ensure_chatgpt_page_is_idempotent(self) -> None:
        context = _Context()
        service, _host, _registry, _browser, _chromium, _runtime, _factory_calls = _make_service(context)
        with self.assertRaises(PlaywrightAttachedBrainError):
            service.connect(connect_attempt_id="ensure-idempotent")

        first = service.ensure_chatgpt_page()
        second = service.ensure_chatgpt_page()

        self.assertEqual(first["action"], "CREATED_NEW_PAGE")
        self.assertEqual(second["action"], "REUSED_EXISTING_PAGE")
        self.assertEqual(len(context.pages), 1)
        self.assertEqual(len(context.created_pages), 1)
        self.assertEqual(context.created_pages[0].operations.count("goto"), 1)

    def test_ensure_chatgpt_page_uses_existing_host_context(self) -> None:
        ordinary_page = _Page("https://example.org/")
        context = _Context([ordinary_page])
        service, host, _registry, browser, _chromium, _runtime, _factory_calls = _make_service(context)
        with self.assertRaises(PlaywrightAttachedBrainError):
            service.connect(connect_attempt_id="ensure-existing-context")

        result = service.ensure_chatgpt_page()

        self.assertEqual(result["action"], "CREATED_NEW_PAGE")
        self.assertIs(host.browser, browser)
        self.assertIs(host.context, context)
        self.assertIn(ordinary_page, context.pages)
        self.assertEqual(len(browser.contexts), 1)
        self.assertEqual(len(context.created_pages), 1)

    def test_ensure_chatgpt_page_single_flight(self) -> None:
        context = _Context()
        service, _host, _registry, _browser, _chromium, _runtime, _factory_calls = _make_service(context)
        with self.assertRaises(PlaywrightAttachedBrainError):
            service.connect(connect_attempt_id="ensure-single-flight")
        nested_errors: list[str] = []

        def reenter_ensure() -> None:
            try:
                service.ensure_chatgpt_page()
            except PlaywrightAttachedBrainError as exc:
                nested_errors.append(exc.code)

        context.on_new_page = reenter_ensure
        result = service.ensure_chatgpt_page()

        self.assertEqual(result["action"], "CREATED_NEW_PAGE")
        self.assertEqual(nested_errors, ["ENSURE_CHATGPT_PAGE_IN_PROGRESS"])
        self.assertEqual(len(context.created_pages), 1)

    def test_ensure_chatgpt_page_only_allows_chatgpt_target(self) -> None:
        class _RouteApp:
            calls = 0

            def ensure_chatgpt_page(self):
                self.calls += 1
                return {"action": "CREATED_NEW_PAGE"}

        app = _RouteApp()
        server = make_server(app, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/api/brain/real-chrome/ensure-chatgpt-page",
                data=json.dumps({"url": "https://example.org/"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as response:
                urlopen(request, timeout=2)
            self.assertEqual(response.exception.code, 400)
            self.assertEqual(app.calls, 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_product_shell_ensure_route_calls_only_formal_operation(self) -> None:
        class _RouteApp:
            def __init__(self) -> None:
                self.calls = 0

            def ensure_chatgpt_page(self):
                self.calls += 1
                return {"action": "REUSED_EXISTING_PAGE", "boundPageId": "page-0"}

        app = _RouteApp()
        server = make_server(app, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/api/brain/real-chrome/ensure-chatgpt-page",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=2) as response:
                payload = json.load(response)
            self.assertEqual(payload["action"], "REUSED_EXISTING_PAGE")
            self.assertEqual(app.calls, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_ensure_chatgpt_page_does_not_create_second_runtime(self) -> None:
        context = _Context()
        service, host, registry, browser, chromium, runtime, factory_calls = _make_service(
            context, fail_factory_on_repeat=True
        )
        with self.assertRaises(PlaywrightAttachedBrainError):
            service.connect(connect_attempt_id="ensure-one-runtime")

        service.ensure_chatgpt_page()

        self.assertIs(service.registry, registry)
        self.assertIs(registry.get(), host)
        self.assertIs(host.browser, browser)
        self.assertIs(host.playwright, runtime)
        self.assertEqual(factory_calls(), 1)
        self.assertEqual(chromium.connect_calls, 1)

    def test_page_creation_runs_on_playwright_owner_thread(self) -> None:
        context = _Context()
        service, host, _registry, _browser, _chromium, _runtime, _factory_calls = _make_service(context)
        with self.assertRaises(PlaywrightAttachedBrainError):
            service.connect(connect_attempt_id="ensure-owner-thread")
        owner_thread_id = host.owner_thread_id

        service.ensure_chatgpt_page()

        self.assertEqual(context.created_thread_ids, [owner_thread_id])
        self.assertEqual(context.created_pages[0].goto_thread_id, owner_thread_id)
        self.assertEqual(host._thread_operation_matrix["ensure_chatgpt_page"]["callerThreadId"], owner_thread_id)

    def test_page_creation_does_not_trigger_sync_inside_asyncio(self) -> None:
        context = _Context()
        service, host, _registry, browser, chromium, runtime, factory_calls = _make_service(
            context, fail_factory_on_repeat=True
        )
        with self.assertRaises(PlaywrightAttachedBrainError):
            service.connect(connect_attempt_id="ensure-no-second-sync-runtime")

        result = service.ensure_chatgpt_page()

        self.assertEqual(result["action"], "CREATED_NEW_PAGE")
        self.assertEqual(factory_calls(), 1)
        self.assertEqual(chromium.connect_calls, 1)
        self.assertIs(host.browser, browser)
        self.assertIs(host.playwright, runtime)

    def test_ensure_page_does_not_send_message(self) -> None:
        context = _Context()
        service, _host, _registry, _browser, _chromium, _runtime, _factory_calls = _make_service(context)
        with self.assertRaises(PlaywrightAttachedBrainError):
            service.connect(connect_attempt_id="ensure-no-message")

        service.ensure_chatgpt_page()

        self.assertEqual(context.created_pages[0].operations.count("goto"), 1)
        self.assertFalse(any(operation in {"fill", "press", "click", "send", "submit"} for operation in context.created_pages[0].operations))


if __name__ == "__main__":
    unittest.main()
