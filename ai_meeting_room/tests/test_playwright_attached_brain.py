from __future__ import annotations

import inspect
import unittest
from pathlib import Path

from ai_meeting_room.brain.playwright_attached_brain import (
    PLAYWRIGHT_ATTACHED_BROWSER_RUNTIME,
    PLAYWRIGHT_CHANNEL,
    PLAYWRIGHT_LEGACY_CDP_URL,
    PLAYWRIGHT_CDP_ENDPOINT,
    PlaywrightAttachedBrainHost,
    PlaywrightAttachedBrainState,
    PlaywrightAttachedBrainTransport,
    PlaywrightChatGPTPageResolver,
    PlaywrightAttachedBrainError,
    chromium_channel_support,
    _safe_text,
)
from ai_meeting_room.brain.playwright_brain import ChatGPTPlaywrightDomAdapter


class FakeLocator:
    def __init__(self, count: int = 0, editable: bool = True) -> None:
        self._count = count
        self._editable = editable

    def count(self) -> int:
        return self._count

    def nth(self, _index: int) -> "FakeLocator":
        return self

    def is_visible(self) -> bool:
        return True

    def is_editable(self) -> bool:
        return self._editable


class FakePage:
    def __init__(self, url: str, composer: bool = False) -> None:
        self.url = url
        self.composer = composer
        self.handlers: dict[str, object] = {}
        self.locator_calls = 0

    def on(self, event: str, callback) -> None:
        self.handlers[event] = callback

    def locator(self, selector: str) -> FakeLocator:
        self.locator_calls += 1
        if selector in ChatGPTPlaywrightDomAdapter.COMPOSER_SELECTORS:
            return FakeLocator(int(self.composer))
        if selector in ChatGPTPlaywrightDomAdapter.APP_SHELL_SELECTORS:
            return FakeLocator(1)
        return FakeLocator(0)

    def get_by_text(self, _pattern) -> FakeLocator:
        return FakeLocator(0)

    def evaluate(self, _expression: str):
        return "complete"


class FakeContext:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages


class FakeBrowser:
    def __init__(self, contexts: list[FakeContext]) -> None:
        self.contexts = contexts
        self.handlers: dict[str, object] = {}

    def on(self, event: str, callback) -> None:
        self.handlers[event] = callback


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.endpoint = None

    def connect_over_cdp(self, endpoint: str) -> FakeBrowser:
        self.endpoint = endpoint
        return self.browser


class FakePlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeChromium(browser)
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class PlaywrightAttachedBrainTests(unittest.TestCase):
    def make_runtime(self, pages: list[FakePage]):
        browser = FakeBrowser([FakeContext(pages)])
        runtime = FakePlaywright(browser)
        host = PlaywrightAttachedBrainHost(
            playwright_factory=lambda: runtime,
            cdp_preflight=lambda _endpoint: {"status": 200, "shape": "VALID_WITH_WEBSOCKET_URL"},
        )
        return host, runtime, browser

    def test_formal_runtime_uses_playwright_connect_over_cdp(self) -> None:
        source = inspect.getsource(PlaywrightAttachedBrainHost.connect)
        self.assertIn("connect_over_cdp", source)
        self.assertEqual(PLAYWRIGHT_CHANNEL, "chrome")
        self.assertEqual(PLAYWRIGHT_CDP_ENDPOINT, "chrome")
        self.assertEqual(PLAYWRIGHT_LEGACY_CDP_URL, "http://127.0.0.1:9222")

    def test_formal_runtime_does_not_start_browser(self) -> None:
        source = inspect.getsource(PlaywrightAttachedBrainHost)
        self.assertNotIn("launch(", source)
        self.assertNotIn("launch_persistent_context", source)
        ensure_source = inspect.getsource(PlaywrightAttachedBrainHost.ensure_chatgpt_page)
        self.assertIn("context.new_page()", ensure_source)
        self.assertIn("page.goto(CHATGPT_URL", ensure_source)
        self.assertNotIn("url", inspect.signature(PlaywrightAttachedBrainHost.ensure_chatgpt_page).parameters)
        self.assertNotIn("launch(", ensure_source)

    def test_mcp_not_used_in_formal_runtime(self) -> None:
        source = inspect.getsource(PlaywrightAttachedBrainHost) + inspect.getsource(PlaywrightAttachedBrainTransport)
        self.assertNotIn("MCP", source)
        self.assertNotIn("list_pages", source)
        self.assertNotIn("structuredContent", source)

    def test_existing_browser_context_is_used(self) -> None:
        page = FakePage("https://chatgpt.com/", composer=True)
        host, runtime, browser = self.make_runtime([page])
        host.connect()
        self.assertIs(host.browser, browser)
        self.assertIs(host.context, browser.contexts[0])
        self.assertIs(host.page, page)
        self.assertEqual(runtime.chromium.endpoint, "ws://127.0.0.1:1/devtools/browser/TEST")

    def test_chatgpt_page_selected_from_context_pages(self) -> None:
        non_chatgpt = FakePage("https://example.com/", composer=True)
        target = FakePage("https://chatgpt.com/c/example", composer=True)
        host, _, _ = self.make_runtime([non_chatgpt, target])
        host.connect()
        self.assertIs(host.page, target)
        self.assertEqual(non_chatgpt.locator_calls, 0)

    def test_chrome_internal_page_ignored(self) -> None:
        resolver = PlaywrightChatGPTPageResolver()
        self.assertIsNone(resolver.resolve([FakePage("chrome://inspect/#remote-debugging")]))

    def test_non_chatgpt_page_not_read(self) -> None:
        page = FakePage("https://gmail.com/", composer=True)
        host, _, _ = self.make_runtime([page])
        with self.assertRaisesRegex(Exception, "CHATGPT_TARGET_NOT_FOUND"):
            host.connect()
        self.assertEqual(page.locator_calls, 0)

    def test_composer_locator_fallback(self) -> None:
        self.assertIn('[data-testid="prompt-textarea"]', ChatGPTPlaywrightDomAdapter.COMPOSER_SELECTORS)
        self.assertIn('[contenteditable="true"]', ChatGPTPlaywrightDomAdapter.COMPOSER_SELECTORS)
        page = FakePage("https://chatgpt.com/", composer=True)
        host, _, _ = self.make_runtime([page])
        host.connect()
        self.assertTrue(host.status()["composerFound"])

    def test_browser_disconnect_fail_closed(self) -> None:
        failures = []
        page = FakePage("https://chatgpt.com/", composer=True)
        host, _, browser = self.make_runtime([page])
        host.on_failure = lambda state, reason: failures.append((state, reason))
        host.connect()
        browser.handlers["disconnected"]()
        self.assertEqual(host.state, PlaywrightAttachedBrainState.BROWSER_LOST)
        self.assertTrue(failures)

    def test_chatgpt_page_close_fail_closed(self) -> None:
        page = FakePage("https://chatgpt.com/", composer=True)
        host, _, _ = self.make_runtime([page])
        host.connect()
        page.handlers["close"]()
        self.assertEqual(host.state, PlaywrightAttachedBrainState.PAGE_LOST)

    def test_navigation_away_fail_closed(self) -> None:
        page = FakePage("https://chatgpt.com/", composer=True)
        host, _, _ = self.make_runtime([page])
        host.connect()
        page.url = "https://example.com/"
        page.handlers["framenavigated"]()
        self.assertEqual(host.state, PlaywrightAttachedBrainState.PAGE_LOST)

    def test_no_cookie_read(self) -> None:
        source = inspect.getsource(PlaywrightAttachedBrainHost) + inspect.getsource(PlaywrightAttachedBrainTransport)
        self.assertNotIn("get_cookies", source)
        self.assertNotIn("document.cookie", source)
        self.assertIn('"cookieReadByApp": "NONE"', source)

    def test_no_token_read(self) -> None:
        source = inspect.getsource(PlaywrightAttachedBrainHost) + inspect.getsource(PlaywrightAttachedBrainTransport)
        self.assertNotIn("storage_state", source)
        self.assertNotIn("Authorization", source)
        self.assertIn('"tokenReadByApp": "NONE"', source)

    def test_ready_requires_existing_chatgpt_composer(self) -> None:
        host, _, _ = self.make_runtime([FakePage("https://chatgpt.com/", composer=False)])
        with self.assertRaisesRegex(Exception, "LOADING|DOM_UNKNOWN|COMPOSER_NOT_FOUND"):
            host.connect()
        self.assertNotEqual(host.state, PlaywrightAttachedBrainState.READY)
        self.assertEqual(PLAYWRIGHT_ATTACHED_BROWSER_RUNTIME, "PLAYWRIGHT_ATTACH_EXISTING_REAL_CHROME")

    def test_connect_error_preserves_error_name(self) -> None:
        class FailingChromium:
            def connect_over_cdp(self, _endpoint):
                raise RuntimeError("socket refused")
        class Runtime:
            chromium = FailingChromium()
        host = PlaywrightAttachedBrainHost(
            playwright_factory=lambda: Runtime(),
            cdp_preflight=lambda _endpoint: {"status": 200, "shape": "VALID_WITH_WEBSOCKET_URL"},
        )
        with self.assertRaises(PlaywrightAttachedBrainError) as caught:
            host.connect("attempt-error-name")
        self.assertEqual(caught.exception.original_name, "RuntimeError")

    def test_connect_error_preserves_error_message(self) -> None:
        class FailingChromium:
            def connect_over_cdp(self, _endpoint):
                raise RuntimeError("socket refused")
        class Runtime:
            chromium = FailingChromium()
        host = PlaywrightAttachedBrainHost(playwright_factory=lambda: Runtime(), cdp_preflight=lambda _: {"status": 200, "shape": "VALID_WITH_WEBSOCKET_URL"})
        with self.assertRaises(PlaywrightAttachedBrainError) as caught:
            host.connect("attempt-error-message")
        self.assertIn("socket refused", caught.exception.envelope("attempt-error-message")["message"])

    def test_connect_error_has_stage(self) -> None:
        class FailingChromium:
            def connect_over_cdp(self, _endpoint):
                raise RuntimeError("socket refused")
        class Runtime:
            chromium = FailingChromium()
        host = PlaywrightAttachedBrainHost(playwright_factory=lambda: Runtime(), cdp_preflight=lambda _: {"status": 200, "shape": "VALID_WITH_WEBSOCKET_URL"})
        with self.assertRaises(PlaywrightAttachedBrainError) as caught:
            host.connect("attempt-stage")
        self.assertEqual(caught.exception.stage, "T5_CONNECT_OVER_CDP_START")

    def test_connect_error_has_attempt_id(self) -> None:
        class FailingChromium:
            def connect_over_cdp(self, _endpoint):
                raise RuntimeError("socket refused")
        class Runtime:
            chromium = FailingChromium()
        host = PlaywrightAttachedBrainHost(playwright_factory=lambda: Runtime(), cdp_preflight=lambda _: {"status": 200, "shape": "VALID_WITH_WEBSOCKET_URL"})
        with self.assertRaises(PlaywrightAttachedBrainError):
            host.connect("attempt-id")
        self.assertEqual(host.status()["connectAttemptId"], "attempt-id")
        self.assertEqual(host.status()["lastErrorDetails"]["attemptId"], "attempt-id")

    def test_cdp_http_preflight_failure_is_explicit(self) -> None:
        def failed_preflight(_endpoint):
            raise PlaywrightAttachedBrainError("connection refused", code="CDP_ENDPOINT_UNREACHABLE", stage="T5_CONNECT_OVER_CDP_START")
        host = PlaywrightAttachedBrainHost(cdp_endpoint=PLAYWRIGHT_LEGACY_CDP_URL, playwright_factory=lambda: object(), cdp_preflight=failed_preflight)
        with self.assertRaises(PlaywrightAttachedBrainError) as caught:
            host.connect("attempt-cdp")
        self.assertEqual(caught.exception.code, "CDP_ENDPOINT_UNREACHABLE")
        self.assertEqual(host.status()["lastErrorCode"], "CDP_ENDPOINT_UNREACHABLE")

    def test_connect_over_cdp_error_is_not_swallowed(self) -> None:
        class FailingChromium:
            def connect_over_cdp(self, _endpoint):
                raise ValueError("Playwright transport failure")
        class Runtime:
            chromium = FailingChromium()
        host = PlaywrightAttachedBrainHost(playwright_factory=lambda: Runtime(), cdp_preflight=lambda _: {"status": 200, "shape": "VALID_WITH_WEBSOCKET_URL"})
        with self.assertRaises(PlaywrightAttachedBrainError) as caught:
            host.connect("attempt-cdp-error")
        self.assertEqual(caught.exception.code, "PLAYWRIGHT_EXACT_WS_CONNECT_FAILED")
        self.assertIn("Playwright transport failure", caught.exception.envelope("attempt-cdp-error")["message"])

    def test_channel_connect_timeout_is_explicit_and_configured(self) -> None:
        class TimingOutChromium:
            def __init__(self) -> None:
                self.timeout = None

            def connect_over_cdp(self, _endpoint, *, timeout=None):
                self.timeout = timeout
                raise TimeoutError("connect timed out")

        chromium = TimingOutChromium()
        class Runtime:
            def __init__(self) -> None:
                self.chromium = chromium
        host = PlaywrightAttachedBrainHost(
            cdp_endpoint=PLAYWRIGHT_LEGACY_CDP_URL,
            playwright_factory=lambda: Runtime(),
            connect_timeout_ms=1234,
            cdp_preflight=lambda _: {"status": 200, "shape": "VALID_WITH_WEBSOCKET_URL"},
        )
        with self.assertRaises(PlaywrightAttachedBrainError) as caught:
            host.connect("attempt-timeout")
        self.assertEqual(chromium.timeout, 1234)
        self.assertEqual(caught.exception.code, "CHROME_RUNTIME_UNAVAILABLE")

    def test_playwright_version_diagnostics(self) -> None:
        versions = PlaywrightAttachedBrainHost._version_diagnostics()
        self.assertIn("python", versions)
        self.assertIn("playwright", versions)
        self.assertIn("playwrightCore", versions)

    def test_formal_runtime_never_starts_mcp(self) -> None:
        root = Path(__file__).parents[2]
        app_source = (root / "ai_meeting_room" / "product" / "app.py").read_text(encoding="utf-8")
        main_source = (root / "desktop" / "main.js").read_text(encoding="utf-8")
        self.assertNotIn("ChromeDevToolsMcpClient", main_source)
        self.assertIn("PlaywrightAttachedBrainHost", app_source)

    def test_connect_success_records_context_count(self) -> None:
        page = FakePage("https://chatgpt.com/", composer=True)
        host, _, _ = self.make_runtime([page])
        host.connect("attempt-success")
        status = host.status()
        self.assertEqual(status["contextCount"], 1)
        self.assertEqual(status["connectAttemptId"], "attempt-success")
        self.assertEqual(status["lastSuccessStage"], "T15_SUCCESS")

    def test_formal_connection_uses_dynamic_exact_websocket(self) -> None:
        page = FakePage("https://chatgpt.com/", composer=True)
        host, runtime, _ = self.make_runtime([page])
        host.connect("attempt-channel")
        self.assertEqual(runtime.chromium.endpoint, "ws://127.0.0.1:1/devtools/browser/TEST")
        self.assertEqual(host.status()["formalBrowserConnection"], "PLAYWRIGHT_CONNECT_OVER_CDP_EXACT_WS")

    def test_formal_connection_does_not_use_9222_url(self) -> None:
        source = inspect.getsource(PlaywrightAttachedBrainHost._connect_unlocked)
        self.assertNotIn("PLAYWRIGHT_LEGACY_CDP_URL", source)
        self.assertIn("read_devtools_active_port", source)
        self.assertEqual(PLAYWRIGHT_LEGACY_CDP_URL, "http://127.0.0.1:9222")

    def test_exact_mode_skips_json_version_preflight(self) -> None:
        calls = []
        page = FakePage("https://chatgpt.com/", composer=True)
        host, _, _ = self.make_runtime([page])
        host.cdp_preflight = lambda endpoint: calls.append(endpoint) or {"status": 200, "shape": "VALID_WITH_WEBSOCKET_URL"}
        host.connect("attempt-no-http-preflight")
        self.assertEqual(calls, [])
        self.assertEqual(host.status()["cdpHttpPreflight"], "NOT_APPLICABLE")

    def test_playwright_and_core_versions_match(self) -> None:
        versions = PlaywrightAttachedBrainHost._version_diagnostics()
        self.assertEqual(versions["playwright"], versions["playwrightCore"])

    def test_chrome_channel_support_detected(self) -> None:
        support = chromium_channel_support()
        self.assertTrue(support["supported"], support)
        self.assertTrue(support["isChromiumChannelName"])
        self.assertTrue(support["resolveChannelEndpoint"])

    def test_channel_connect_failure_is_explicit(self) -> None:
        class FailingChromium:
            def connect_over_cdp(self, _endpoint):
                raise RuntimeError("Could not connect to chrome. DevToolsActivePort file not found")
        class Runtime:
            chromium = FailingChromium()
        host = PlaywrightAttachedBrainHost(playwright_factory=lambda: Runtime())
        with self.assertRaises(PlaywrightAttachedBrainError) as caught:
            host.connect("attempt-channel-error")
        self.assertEqual(caught.exception.code, "PLAYWRIGHT_EXACT_WS_CONNECT_FAILED")

    def test_mcp_not_started(self) -> None:
        root = Path(__file__).parents[2]
        main_source = (root / "desktop" / "main.js").read_text(encoding="utf-8")
        app_source = (root / "ai_meeting_room" / "product" / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("ChromeDevToolsMcpClient", main_source)
        self.assertIn('"MCP_PROCESS_STARTED": False', app_source)

    def test_browser_not_launched_by_app(self) -> None:
        source = inspect.getsource(PlaywrightAttachedBrainHost)
        self.assertNotIn("launch_persistent_context", source)
        self.assertNotIn("chromium.launch", source)

    def test_chatgpt_page_policy_preserved(self) -> None:
        host = PlaywrightAttachedBrainHost(playwright_factory=lambda: object())
        self.assertEqual(host.status()["applicationTabPolicy"], "CHATGPT_ONLY")
        self.assertEqual(host.status()["otherTabReadByApp"], "NONE")

    def test_cookie_not_read(self) -> None:
        source = inspect.getsource(PlaywrightAttachedBrainHost)
        self.assertNotIn("get_cookies", source)

    def test_token_not_read(self) -> None:
        source = inspect.getsource(PlaywrightAttachedBrainHost)
        self.assertNotIn("storage_state", source)

    def test_chatgpt_page_failure_is_explicit(self) -> None:
        host, _, _ = self.make_runtime([FakePage("https://example.com/", composer=True)])
        with self.assertRaises(PlaywrightAttachedBrainError) as caught:
            host.connect("attempt-page")
        self.assertEqual(caught.exception.code, "CHATGPT_PAGE_NOT_FOUND")

    def test_composer_failure_is_explicit(self) -> None:
        host, _, _ = self.make_runtime([FakePage("https://chatgpt.com/", composer=False)])
        with self.assertRaises(PlaywrightAttachedBrainError) as caught:
            host.connect("attempt-composer")
        self.assertEqual(caught.exception.code, "COMPOSER_NOT_FOUND")

    def test_logs_do_not_contain_cookie(self) -> None:
        self.assertEqual(_safe_text("Cookie=secret"), "Cookie=[REDACTED]")

    def test_logs_do_not_contain_token(self) -> None:
        self.assertEqual(_safe_text("access_token=secret"), "access_token=[REDACTED]")


if __name__ == "__main__":
    unittest.main()
