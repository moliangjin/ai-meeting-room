from __future__ import annotations

import asyncio
import inspect
import io
import threading
import unittest
from types import SimpleNamespace

from ai_meeting_room.brain.playwright_attached_brain import PlaywrightAttachedBrainHost
from ai_meeting_room.brain.runtime_registry import FormalBrainRuntimeRegistry, FormalBrainRuntimeService
from ai_meeting_room.product.app import Phase2Application
from ai_meeting_room.product.server import ProductHandler


class _PageFocusOnly:
    """A deliberately tiny page double: only focus is a permitted operation."""

    def __init__(self) -> None:
        self.focus_calls = 0
        self.other_accesses: list[str] = []

    def bring_to_front(self) -> None:
        self.focus_calls += 1

    def __getattr__(self, name: str):
        self.other_accesses.append(name)
        raise AssertionError(f"foreground operation accessed forbidden Page member: {name}")


def _formal_service(*, bound: bool = True):
    factory_calls: list[str] = []

    def playwright_factory():
        factory_calls.append("created")
        raise AssertionError("foreground operation must not create a Playwright runtime")

    host = PlaywrightAttachedBrainHost(playwright_factory=playwright_factory)
    page = _PageFocusOnly() if bound else None
    host.page = page
    host.bound_page_id = "page-0" if bound else None
    registry = FormalBrainRuntimeRegistry(host)
    service = FormalBrainRuntimeService(registry)
    return service, registry, host, page, factory_calls


class BoundPageForegroundTests(unittest.TestCase):
    def test_bring_bound_page_to_front_uses_current_bound_page(self) -> None:
        service, registry, host, page, _factory_calls = _formal_service()
        original_registry = registry
        original_host = registry.get()

        result = service.bring_bound_page_to_front()

        self.assertIs(registry, original_registry)
        self.assertIs(registry.get(), original_host)
        self.assertIs(host.page, page)
        self.assertEqual(page.focus_calls, 1)
        self.assertEqual(result["boundPageId"], "page-0")
        self.assertTrue(result["ok"])

    def test_bring_bound_page_to_front_rejects_when_no_bound_page(self) -> None:
        service, _registry, host, _page, _factory_calls = _formal_service(bound=False)

        result = service.bring_bound_page_to_front()

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "NO_BOUND_PAGE")
        self.assertIsNone(host.page)

    def test_bring_bound_page_to_front_accepts_no_page_id(self) -> None:
        parameters = inspect.signature(PlaywrightAttachedBrainHost.bring_bound_page_to_front).parameters
        self.assertEqual(tuple(parameters), ("self",))

    def test_bring_bound_page_to_front_accepts_no_url(self) -> None:
        parameters = inspect.signature(PlaywrightAttachedBrainHost.bring_bound_page_to_front).parameters
        self.assertNotIn("url", parameters)

    def test_bring_bound_page_to_front_accepts_no_selector(self) -> None:
        parameters = inspect.signature(PlaywrightAttachedBrainHost.bring_bound_page_to_front).parameters
        self.assertNotIn("selector", parameters)

    def test_bring_bound_page_to_front_does_not_resolve_page(self) -> None:
        service, _registry, host, page, _factory_calls = _formal_service()

        class ForbiddenResolver:
            def resolve_with_diagnostics(self, *_args, **_kwargs):
                raise AssertionError("foreground operation must not resolve pages")

        host.page_resolver = ForbiddenResolver()
        result = service.bring_bound_page_to_front()

        self.assertTrue(result["ok"])
        self.assertEqual(page.focus_calls, 1)

    def test_bring_bound_page_to_front_does_not_read_dom(self) -> None:
        service, _registry, _host, page, _factory_calls = _formal_service()

        result = service.bring_bound_page_to_front()

        self.assertTrue(result["ok"])
        self.assertEqual(result["domReadCount"], 0)
        self.assertEqual(page.other_accesses, [])

    def test_bring_bound_page_to_front_does_not_read_text(self) -> None:
        service, _registry, _host, page, _factory_calls = _formal_service()

        result = service.bring_bound_page_to_front()

        self.assertTrue(result["ok"])
        self.assertEqual(result["textReadCount"], 0)
        self.assertEqual(page.other_accesses, [])

    def test_bring_bound_page_to_front_runs_on_owner_thread(self) -> None:
        service, _registry, host, _page, _factory_calls = _formal_service()

        result = service.bring_bound_page_to_front()

        self.assertEqual(result["operationThreadId"], host.owner_thread_id)
        self.assertEqual(result["ownerThreadId"], host.owner_thread_id)
        self.assertEqual(result["operationThreadId"], threading.get_ident())

    def test_bring_bound_page_to_front_rejects_non_owner_thread(self) -> None:
        service, _registry, _host, page, _factory_calls = _formal_service()
        failures: list[Exception] = []

        def call_from_non_owner():
            try:
                service.bring_bound_page_to_front()
            except Exception as exc:
                failures.append(exc)

        thread = threading.Thread(target=call_from_non_owner)
        thread.start()
        thread.join()

        self.assertEqual(getattr(failures[0], "code", None), "PLAYWRIGHT_THREAD_AFFINITY_VIOLATION")
        self.assertEqual(page.focus_calls, 0)

    def test_bring_bound_page_to_front_rejects_running_asyncio_loop(self) -> None:
        service, _registry, host, _page, _factory_calls = _formal_service()

        async def call_from_running_loop():
            return service.bring_bound_page_to_front()

        result = asyncio.run(call_from_running_loop())

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "PLAYWRIGHT_SYNC_API_INSIDE_ASYNCIO_LOOP")
        self.assertEqual(result["operationThreadId"], host.owner_thread_id)

    def test_bring_bound_page_to_front_allows_its_playwright_dispatch_loop(self) -> None:
        service, _registry, host, page, _factory_calls = _formal_service()

        async def call_with_playwright_loop():
            host.playwright = SimpleNamespace(_loop=asyncio.get_running_loop())
            return service.bring_bound_page_to_front()

        result = asyncio.run(call_with_playwright_loop())

        self.assertTrue(result["ok"])
        self.assertEqual(page.focus_calls, 1)

    def test_bring_bound_page_to_front_rejects_foreign_asyncio_loop(self) -> None:
        service, _registry, host, page, _factory_calls = _formal_service()
        host.playwright = SimpleNamespace(_loop=object())

        async def call_with_foreign_loop():
            return service.bring_bound_page_to_front()

        result = asyncio.run(call_with_foreign_loop())

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "PLAYWRIGHT_SYNC_API_INSIDE_ASYNCIO_LOOP")
        self.assertEqual(page.focus_calls, 0)

    def test_bring_bound_page_to_front_does_not_create_runtime(self) -> None:
        service, registry, host, _page, factory_calls = _formal_service()
        host_id = registry.host_instance_id
        registry_id = registry.registry_instance_id

        service.bring_bound_page_to_front()

        self.assertEqual(factory_calls, [])
        self.assertEqual(registry.host_instance_id, host_id)
        self.assertEqual(registry.registry_instance_id, registry_id)

    def test_bring_bound_page_to_front_does_not_send_message(self) -> None:
        service, _registry, _host, page, _factory_calls = _formal_service()

        result = service.bring_bound_page_to_front()

        self.assertTrue(result["ok"])
        self.assertEqual(page.focus_calls, 1)
        self.assertEqual(page.other_accesses, [])

    def test_foreground_result_declares_zero_page_reads(self) -> None:
        service, _registry, _host, _page, _factory_calls = _formal_service()

        result = service.bring_bound_page_to_front()

        self.assertEqual(result["pageReadCount"], 0)
        self.assertEqual(result["domReadCount"], 0)
        self.assertEqual(result["textReadCount"], 0)

    def test_product_app_exposes_only_the_fixed_foreground_operation(self) -> None:
        service, _registry, _host, _page, _factory_calls = _formal_service()
        app = Phase2Application.__new__(Phase2Application)
        app.formal_brain_service = service

        result = app.bring_bound_brain_page_to_front()

        self.assertTrue(result["ok"])


class BoundPageForegroundRouteTests(unittest.TestCase):
    def _handler(self, *, app, path: str, body: dict | None = None, client_ip: str = "127.0.0.1"):
        class Handler(ProductHandler):
            def __init__(self):
                self.path = path
                self.app = app
                self.client_address = (client_ip, 12345)
                self.rfile = io.BytesIO()
                self.headers = {"Content-Length": "0"}
                self.server = SimpleNamespace()
                self.response = None
                self._request_body = body or {}

            def _body(self):
                return self._request_body

            def _json(self, status, payload):
                self.response = (status, payload)

        return Handler()

    def test_product_shell_has_fixed_loopback_foreground_route(self) -> None:
        service, _registry, _host, page, _factory_calls = _formal_service()
        app = Phase2Application.__new__(Phase2Application)
        app.formal_brain_service = service
        handler = self._handler(
            app=app,
            path="/api/brain/real-chrome/bring-bound-page-to-front",
        )

        handler.do_POST()

        self.assertEqual(handler.response[0], 200)
        self.assertTrue(handler.response[1]["ok"])
        self.assertEqual(page.focus_calls, 1)

    def test_product_shell_foreground_route_rejects_generic_controls(self) -> None:
        class App:
            def bring_bound_brain_page_to_front(self):
                raise AssertionError("non-empty generic request must be rejected")

        handler = self._handler(
            app=App(),
            path="/api/brain/real-chrome/bring-bound-page-to-front",
            body={"pageId": "page-0", "url": "https://example.invalid", "selector": "body"},
        )

        handler.do_POST()

        self.assertEqual(handler.response[0], 400)
        self.assertEqual(handler.response[1]["error"]["code"], "UNEXPECTED_FOREGROUND_OPTIONS")

    def test_product_shell_foreground_route_is_loopback_only(self) -> None:
        class App:
            def bring_bound_brain_page_to_front(self):
                raise AssertionError("non-loopback caller must be rejected")

        handler = self._handler(
            app=App(),
            path="/api/brain/real-chrome/bring-bound-page-to-front",
            client_ip="192.0.2.10",
        )

        handler.do_POST()

        self.assertEqual(handler.response[0], 403)
        self.assertEqual(handler.response[1]["error"]["code"], "LOCAL_ONLY")


if __name__ == "__main__":
    unittest.main()
