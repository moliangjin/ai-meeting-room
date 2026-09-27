from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_meeting_room.brain.dedicated_cdp import DevToolsActivePortRecord
from ai_meeting_room.brain.playwright_attached_brain import PlaywrightAttachedBrainHost
from ai_meeting_room.brain.runtime_registry import FormalBrainRuntimeRegistry, FormalBrainRuntimeService
from ai_meeting_room.product.server import ProductHandler


class _Page:
    def __init__(self, url: str) -> None:
        self.url = url
        self.closed = False
        self.main_frame = SimpleNamespace(url=url)
        self.frames = [self.main_frame]
        self.handlers: dict[str, object] = {}

    def title(self):
        raise AssertionError("page title is prohibited during URL-only reconnect")

    def evaluate(self, _script):
        raise AssertionError("DOM evaluation is prohibited during URL-only reconnect")

    def is_closed(self):
        return self.closed

    def on(self, event: str, callback):
        self.handlers[event] = callback


class _Context:
    def __init__(self, pages: list[_Page]) -> None:
        self.pages = pages


class _Browser:
    def __init__(self, contexts: list[_Context]) -> None:
        self.contexts = contexts
        self.handlers: dict[str, object] = {}

    def is_connected(self):
        return True

    def on(self, event: str, callback):
        self.handlers[event] = callback


class _Chromium:
    def __init__(self, browser: _Browser) -> None:
        self.browser = browser
        self.calls: list[tuple[str, dict[str, object]]] = []

    def connect_over_cdp(self, endpoint: str, **kwargs):
        self.calls.append((endpoint, kwargs))
        return self.browser


class _Playwright:
    def __init__(self, chromium: _Chromium) -> None:
        self.chromium = chromium

    def stop(self):
        pass


def _record() -> DevToolsActivePortRecord:
    return DevToolsActivePortRecord(
        path=Path("/test/DevToolsActivePort"),
        port=43123,
        ws_path="/devtools/browser/r16-exact-ws",
    )


def _make_service(pages: list[_Page]):
    browser = _Browser([_Context(pages)])
    chromium = _Chromium(browser)
    playwright = _Playwright(chromium)
    factory_calls = 0

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        return playwright

    with patch("ai_meeting_room.brain.playwright_attached_brain.chromium_channel_support", return_value={"supported": True}):
        host = PlaywrightAttachedBrainHost(
            playwright_factory=factory,
            endpoint_resolver=_record,
            attach_mode="EXACT_DEDICATED_CDP",
            endpoint_source="DEVTOOLS_ACTIVE_PORT",
        )
    registry = FormalBrainRuntimeRegistry(host)
    return FormalBrainRuntimeService(registry), host, registry, chromium, lambda: factory_calls


class Round16UrlOnlyReconnectTests(unittest.TestCase):
    def test_reconnect_uses_exact_ws_and_url_only_binding_on_existing_formal_host(self):
        ignored = _Page("https://example.org/")
        selected = _Page("https://chatgpt.com/c/opaque-conversation-id")
        service, host, registry, chromium, factory_calls = _make_service([ignored, selected])

        with patch.object(host, "_find_chatgpt_page", side_effect=AssertionError("legacy text fingerprint prohibited")):
            result = service.connect_url_only_for_diagnostics()

        self.assertEqual(chromium.calls, [("ws://127.0.0.1:43123/devtools/browser/r16-exact-ws", {
            "timeout": 10000,
            "is_local": True,
            "no_defaults": True,
        })])
        self.assertEqual(factory_calls(), 1)
        self.assertIs(host.browser, chromium.browser)
        self.assertIs(host.page, selected)
        self.assertIs(host._bound_page_object, selected)
        self.assertIs(host.context, chromium.browser.contexts[0])
        self.assertEqual(host.state, "CONNECTED")
        self.assertEqual(host.auth_state, "UNKNOWN")
        self.assertEqual(host.bound_page_id, "page-1")
        self.assertEqual(result["connectionMode"], "PLAYWRIGHT_CONNECT_OVER_CDP_EXACT_WS")
        self.assertEqual(result["attachMode"], "EXACT_DEDICATED_CDP")
        self.assertEqual(result["endpointSource"], "DEVTOOLS_ACTIVE_PORT")
        self.assertEqual(result["formalCdpPort"], 43123)
        self.assertEqual(result["formalTcpProbe"]["result"], "PASS")
        self.assertEqual(result["chatgptPageCount"], 1)
        self.assertEqual(result["pageCount"], 2)
        self.assertEqual(result["selectedTargetHostname"], "chatgpt.com")
        self.assertEqual(result["runtimeIdentity"]["registryInstanceId"], registry.registry_instance_id)

    def test_reconnect_can_only_ensure_chatgpt_when_no_existing_chatgpt_page(self):
        service, host, registry, chromium, _factory_calls = _make_service([_Page("https://example.org/")])
        context = chromium.browser.contexts[0]
        created = _Page("about:blank")
        context.new_page = lambda: context.pages.append(created) or created
        created.goto = lambda url, **_kwargs: setattr(created, "url", url)

        result = service.connect_url_only_for_diagnostics()

        self.assertIs(host.page, created)
        self.assertEqual(result["action"], "CREATED_NEW_PAGE")
        self.assertEqual(result["chatgptPageCount"], 1)
        self.assertEqual(result["runtimeIdentity"]["hostInstanceId"], registry.host_instance_id)

    def test_route_is_loopback_only_empty_body_and_delegates_to_formal_service(self):
        class App:
            calls = 0

            def connect_formal_brain_url_only_for_diagnostics(self):
                self.calls += 1
                return {"connected": True, "action": "REUSED_EXISTING_PAGE"}

        app = App()

        class Handler(ProductHandler):
            def __init__(self, address):
                self.path = "/api/brain/real-chrome/diagnostic-connect-and-rebind"
                self.client_address = address
                self.rfile = io.BytesIO(b"{}")
                self.headers = {"Content-Length": "2"}
                self.app = app
                self.server = SimpleNamespace()
                self.response = None

            def _json(self, status, payload):
                self.response = (status, payload)

        denied = Handler(("192.0.2.4", 1))
        denied.do_POST()
        self.assertEqual(denied.response[0], 403)
        self.assertEqual(app.calls, 0)

        allowed = Handler(("127.0.0.1", 1))
        allowed.do_POST()
        self.assertEqual(allowed.response, (200, {"connected": True, "action": "REUSED_EXISTING_PAGE"}))
        self.assertEqual(app.calls, 1)

    def test_route_rejects_options(self):
        class App:
            def connect_formal_brain_url_only_for_diagnostics(self):
                raise AssertionError("route options must be rejected before runtime access")

        class Handler(ProductHandler):
            def __init__(self):
                body = json.dumps({"url": "https://example.org/"}).encode()
                self.path = "/api/brain/real-chrome/diagnostic-connect-and-rebind"
                self.client_address = ("127.0.0.1", 1)
                self.rfile = io.BytesIO(body)
                self.headers = {"Content-Length": str(len(body))}
                self.app = App()
                self.server = SimpleNamespace()
                self.response = None

            def _json(self, status, payload):
                self.response = (status, payload)

        handler = Handler()
        handler.do_POST()
        self.assertEqual(handler.response[0], 400)
        self.assertEqual(handler.response[1]["error"]["code"], "UNEXPECTED_DIAGNOSTIC_CONNECT_OPTIONS")


if __name__ == "__main__":
    unittest.main()
