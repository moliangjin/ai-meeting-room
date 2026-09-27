from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_meeting_room.brain.chatgpt_web import ChatGPTWebBrainBridge
from ai_meeting_room.brain.runtime_registry import FormalBrainRuntimeRegistry, FormalBrainRuntimeService
from ai_meeting_room.persistence.sqlite_store import SQLiteStore


class StubHost:
    host_instance_id = "stub-host"
    owner_thread_id = 17

    def __init__(self, *, fail_open: bool = False) -> None:
        self.fail_open = fail_open
        self.open_calls = 0
        self.connect_calls = 0
        self.close_calls = 0

    def openBrain(self, *, connect_attempt_id=None):
        self.open_calls += 1
        if self.fail_open:
            raise RuntimeError("injected connect failure")
        return {"state": "READY", "connectAttemptId": connect_attempt_id}

    def connect(self):
        self.connect_calls += 1

    def auth_status(self):
        return "AUTHENTICATED"

    def status(self):
        return {"state": "READY", "authState": "AUTHENTICATED"}

    def close(self, **_kwargs):
        self.close_calls += 1

    def stop(self, **_kwargs):
        self.close_calls += 1


class FormalBrainBridgeInitTests(unittest.TestCase):
    def test_successful_connect_creates_started_bridge_for_the_same_host(self) -> None:
        host = StubHost()
        registry = FormalBrainRuntimeRegistry(
            host,
            bridge_factory=lambda runtime_host: ChatGPTWebBrainBridge(runtime_host, response_timeout_seconds=180.0),
        )
        service = FormalBrainRuntimeService(registry)

        result = service.connect(connect_attempt_id="t5-connect")

        bridge = registry.wrapped_bridge
        self.assertIsInstance(bridge, ChatGPTWebBrainBridge)
        self.assertIs(bridge.controller, host)
        self.assertEqual(bridge.collector.timeout_seconds, 180.0)
        self.assertTrue(bridge.health_check())
        self.assertEqual(host.open_calls, 1)
        self.assertEqual(host.connect_calls, 1)
        self.assertEqual(result["authState"], "AUTHENTICATED")

    def test_registry_without_factory_preserves_host_only_behavior(self) -> None:
        host = StubHost()
        registry = FormalBrainRuntimeRegistry(host)
        service = FormalBrainRuntimeService(registry)

        service.connect()

        self.assertIsNone(registry.wrapped_bridge)
        self.assertEqual(host.open_calls, 1)
        self.assertEqual(host.connect_calls, 0)

    def test_repeated_connect_reuses_the_cached_bridge(self) -> None:
        host = StubHost()
        created: list[ChatGPTWebBrainBridge] = []

        def factory(runtime_host):
            bridge = ChatGPTWebBrainBridge(runtime_host)
            created.append(bridge)
            return bridge

        registry = FormalBrainRuntimeRegistry(host, bridge_factory=factory)
        service = FormalBrainRuntimeService(registry)
        service.connect()
        first = registry.wrapped_bridge
        service.connect()

        self.assertIs(registry.wrapped_bridge, first)
        self.assertEqual(len(created), 1)
        self.assertEqual(host.open_calls, 2)
        self.assertEqual(host.connect_calls, 2)

    def test_failed_host_connect_does_not_construct_or_cache_bridge(self) -> None:
        host = StubHost(fail_open=True)
        factory_calls = []
        registry = FormalBrainRuntimeRegistry(
            host,
            bridge_factory=lambda runtime_host: factory_calls.append(runtime_host),
        )
        service = FormalBrainRuntimeService(registry)

        with self.assertRaisesRegex(RuntimeError, "injected connect failure"):
            service.connect()

        self.assertEqual(factory_calls, [])
        self.assertIsNone(registry.wrapped_bridge)

    def test_product_shell_configures_formal_chatgpt_bridge_factory(self) -> None:
        from ai_meeting_room.product.app import Phase2Application

        with tempfile.TemporaryDirectory() as directory:
            host = StubHost()
            with patch("ai_meeting_room.product.app.PlaywrightAttachedBrainHost", return_value=host):
                app = Phase2Application(
                    SQLiteStore(Path(directory) / "state.db"),
                    adapter_factory=lambda **_: None,
                )
            try:
                app.formal_brain_service.connect(connect_attempt_id="t5-product-wiring")
                bridge = app.formal_brain_registry.wrapped_bridge

                self.assertIsInstance(bridge, ChatGPTWebBrainBridge)
                self.assertIs(bridge.controller, host)
                self.assertEqual(bridge.collector.timeout_seconds, 180.0)
                self.assertTrue(bridge.health_check())
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
