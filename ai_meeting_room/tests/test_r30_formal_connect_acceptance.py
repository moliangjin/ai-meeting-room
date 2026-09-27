from __future__ import annotations

import unittest
from pathlib import Path

from ai_meeting_room.brain.dedicated_cdp import DevToolsActivePortRecord
from ai_meeting_room.brain.playwright_attached_brain import PlaywrightAttachedBrainHost
from ai_meeting_room.brain.runtime_registry import FormalBrainRuntimeRegistry, FormalBrainRuntimeService


class _Page:
    url = "https://chatgpt.com/"

    def on(self, *_args):
        pass

    def evaluate(self, _script):
        return {
            "visibilityState": "visible",
            "documentReadyState": "complete",
            "loginUiDetected": False,
            "authenticatedShellDetected": True,
            "composerCandidateCount": 1,
            "visibleComposerCount": 1,
            "editableComposerCount": 1,
        }

    def is_closed(self):
        return False


class _Context:
    pages = [_Page()]


class _Browser:
    contexts = [_Context()]

    def on(self, *_args):
        pass

    def is_connected(self):
        return True


class _Chromium:
    def __init__(self, *, error: BaseException | None = None):
        self.error = error
        self.calls: list[dict[str, object]] = []

    def connect_over_cdp(
        self,
        endpoint_url: str,
        *,
        timeout: int,
        is_local: bool,
        no_defaults: bool,
    ):
        self.calls.append({
            "endpoint": endpoint_url,
            "timeout": timeout,
            "is_local": is_local,
            "no_defaults": no_defaults,
        })
        if self.error is not None:
            raise self.error
        return _Browser()


class _Playwright:
    def __init__(self, chromium: _Chromium):
        self.chromium = chromium

    def stop(self):
        pass


def _record(port: int, marker: str) -> DevToolsActivePortRecord:
    return DevToolsActivePortRecord(
        path=Path("/test/DevToolsActivePort"),
        port=port,
        ws_path=f"/devtools/browser/{marker}",
    )


def _make_service(chromium: _Chromium, resolver):
    runtime = _Playwright(chromium)
    host = PlaywrightAttachedBrainHost(
        playwright_factory=lambda: runtime,
        endpoint_resolver=resolver,
        attach_mode="EXACT_DEDICATED_CDP",
        endpoint_source="DEVTOOLS_ACTIVE_PORT",
    )
    registry = FormalBrainRuntimeRegistry(host)
    return FormalBrainRuntimeService(registry), host, registry


class R30FormalConnectAcceptanceTests(unittest.TestCase):
    def test_formal_connect_reuses_live_browser_when_chatgpt_page_appears(self):
        context = type("MutableContext", (), {"pages": []})()

        class ExistingBrowser:
            contexts = [context]

            def on(self, *_args):
                pass

            def is_connected(self):
                return True

        browser = ExistingBrowser()

        class ExistingChromium:
            calls = 0

            def connect_over_cdp(self, _endpoint_url, **_kwargs):
                self.calls += 1
                if self.calls > 1:
                    raise AssertionError("a live formal Browser connection must be reused")
                return browser

        chromium = ExistingChromium()
        runtime = _Playwright(chromium)
        factory_calls = 0

        def runtime_factory():
            nonlocal factory_calls
            factory_calls += 1
            if factory_calls > 1:
                raise RuntimeError("It looks like you are using Playwright Sync API inside the asyncio loop")
            return runtime

        host = PlaywrightAttachedBrainHost(
            playwright_factory=runtime_factory,
            endpoint_resolver=lambda: _record(41020, "same-live-browser"),
            attach_mode="EXACT_DEDICATED_CDP",
            endpoint_source="DEVTOOLS_ACTIVE_PORT",
        )
        registry = FormalBrainRuntimeRegistry(host)
        service = FormalBrainRuntimeService(registry)

        with self.assertRaisesRegex(Exception, "CHATGPT_TARGET_NOT_FOUND"):
            service.connect(connect_attempt_id="first-no-chatgpt-page")
        context.pages.append(_Page())

        result = service.connect(connect_attempt_id="reuse-same-browser")

        self.assertEqual(result["state"], "READY")
        self.assertEqual(result["authState"], "AUTHENTICATED")
        self.assertEqual(chromium.calls, 1)
        self.assertEqual(factory_calls, 1)
        self.assertIs(host.browser, browser)
        self.assertEqual(host.registry_instance_id, registry.registry_instance_id)

    def test_formal_exact_connect_passes_is_local(self):
        chromium = _Chromium()
        service, _host, _registry = _make_service(chromium, lambda: _record(41001, "test-browser-a"))
        service.connect(connect_attempt_id="r30-is-local")
        self.assertIs(chromium.calls[0]["is_local"], True)

    def test_formal_exact_connect_passes_no_defaults(self):
        chromium = _Chromium()
        service, _host, _registry = _make_service(chromium, lambda: _record(41002, "test-browser-b"))
        service.connect(connect_attempt_id="r30-no-defaults")
        self.assertIs(chromium.calls[0]["no_defaults"], True)

    def test_formal_effective_options_match_diagnostic_success_path(self):
        chromium = _Chromium()
        service, host, _registry = _make_service(chromium, lambda: _record(41003, "test-browser-c"))
        service.connect(connect_attempt_id="r30-options")
        actual = chromium.calls[0]
        options = host.status()["formalEffectiveConnectOptions"]
        self.assertEqual(
            {key: actual[key] for key in ("timeout", "is_local", "no_defaults")},
            {"timeout": 10000, "is_local": True, "no_defaults": True},
        )
        self.assertEqual(
            {key: options[key] for key in ("endpointKind", "endpointSource", "timeoutMs", "isLocal", "noDefaults", "headers", "slowMo")},
            {
                "endpointKind": "EXACT_WS",
                "endpointSource": "DEVTOOLS_ACTIVE_PORT",
                "timeoutMs": actual["timeout"],
                "isLocal": actual["is_local"],
                "noDefaults": actual["no_defaults"],
                "headers": "NOT_PASSED",
                "slowMo": "NOT_PASSED",
            },
        )

    def test_formal_connect_uses_fresh_devtools_active_port(self):
        records = iter((_record(41004, "first-browser"), _record(41005, "second-browser")))
        chromium = _Chromium()
        service, host, _registry = _make_service(chromium, lambda: next(records))
        service.connect(connect_attempt_id="r30-fresh-1")
        host.close()
        service.connect(connect_attempt_id="r30-fresh-2")
        self.assertEqual([call["endpoint"] for call in chromium.calls], [
            "ws://127.0.0.1:41004/devtools/browser/first-browser",
            "ws://127.0.0.1:41005/devtools/browser/second-browser",
        ])
        self.assertEqual(host.status()["formalEndpointFreshness"], "CURRENT")

    def test_formal_connect_never_uses_cached_ws_after_restart(self):
        records = iter((_record(41006, "old-browser"), _record(41007, "new-browser")))
        resolver = lambda: next(records)
        first_chromium = _Chromium()
        first_service, first_host, _registry = _make_service(first_chromium, resolver)
        first_service.connect(connect_attempt_id="r30-before-restart")
        first_host.close()
        second_chromium = _Chromium()
        second_service, _second_host, _second_registry = _make_service(second_chromium, resolver)
        second_service.connect(connect_attempt_id="r30-after-restart")
        self.assertEqual(first_chromium.calls[0]["endpoint"], "ws://127.0.0.1:41006/devtools/browser/old-browser")
        self.assertEqual(second_chromium.calls[0]["endpoint"], "ws://127.0.0.1:41007/devtools/browser/new-browser")

    def test_formal_failure_reports_first_stage_and_original_exception(self):
        chromium = _Chromium(error=TimeoutError("connect timeout ws://127.0.0.1:41008/devtools/browser/private-id"))
        service, host, _registry = _make_service(chromium, lambda: _record(41008, "private-id"))
        with self.assertRaises(Exception):
            service.connect(connect_attempt_id="r30-first-failure")
        failure = host.status()["formalConnectFailure"]
        self.assertEqual(failure["stage"], "CONNECT_OVER_CDP")
        self.assertEqual(failure["errorType"], "TimeoutError")
        self.assertIn("connect timeout", failure["errorMessage"])
        self.assertNotIn("private-id", failure["errorMessage"])
        self.assertNotEqual(failure["stackId"], "UNKNOWN")

    def test_formal_connect_runtime_identity_matches(self):
        chromium = _Chromium()
        service, host, registry = _make_service(chromium, lambda: _record(41009, "identity-browser"))
        connected = service.connect(connect_attempt_id="r30-identity")
        identity = service.readonly_identity()
        self.assertEqual(connected["registryInstanceId"], identity["registryInstanceId"])
        self.assertEqual(connected["hostInstanceId"], identity["hostInstanceId"])
        self.assertEqual(connected["boundPageId"], identity["boundPageId"])
        self.assertEqual(connected["ownerThreadId"], identity["ownerThreadId"])
        self.assertEqual(identity["registryInstanceId"], registry.registry_instance_id)
        self.assertEqual(identity["hostInstanceId"], host.host_instance_id)


if __name__ == "__main__":
    unittest.main()
