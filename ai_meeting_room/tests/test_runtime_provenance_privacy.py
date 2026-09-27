from __future__ import annotations

import os
import json
import re
import io
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from ai_meeting_room.brain.runtime_registry import FormalBrainRuntimeRegistry, FormalBrainRuntimeService
from ai_meeting_room.product.app import Phase2Application
from ai_meeting_room.product.server import ProductHandler
from ai_meeting_room.runtime.identity import RuntimeIdentity


class _ForbiddenRuntimeSurface:
    host_instance_id = "host-provenance-test"
    owner_thread_id = 4101
    attach_mode = "EXACT_DEDICATED_CDP"
    endpoint_source = "DEVTOOLS_ACTIVE_PORT"

    def __init__(self) -> None:
        self.health_calls = 0

    @property
    def page(self):
        raise AssertionError("runtime provenance must not access Page")

    @property
    def context(self):
        raise AssertionError("runtime provenance must not access BrowserContext")

    @property
    def browser(self):
        raise AssertionError("runtime provenance must not access Browser")

    def live_poc_health_check(self):
        self.health_calls += 1
        raise AssertionError("runtime provenance must not run live health checks")

    def status(self):
        raise AssertionError("runtime provenance must not request Host health status")


def _app_with_forbidden_runtime() -> tuple[Phase2Application, _ForbiddenRuntimeSurface]:
    app = Phase2Application.__new__(Phase2Application)
    root = Path(__file__).resolve().parents[2]
    app._runtime_identity = RuntimeIdentity.create(root)
    host = _ForbiddenRuntimeSurface()
    registry = FormalBrainRuntimeRegistry(host)
    app.formal_brain_registry = registry
    app.formal_brain_service = FormalBrainRuntimeService(registry)
    return app, host


class RuntimeProvenancePrivacyTests(unittest.TestCase):
    def test_runtime_provenance_does_not_call_live_poc_health_check(self) -> None:
        app, host = _app_with_forbidden_runtime()

        result = app.get_runtime_provenance()

        self.assertEqual(host.health_calls, 0)
        self.assertEqual(result["schemaVersion"], "P3A_RUNTIME_PROVENANCE_V1")

    def test_runtime_provenance_does_not_touch_page(self) -> None:
        app, _host = _app_with_forbidden_runtime()

        result = app.get_runtime_provenance()

        self.assertEqual(result["privacy"]["pageAccessCount"], 0)

    def test_runtime_provenance_does_not_touch_browser_context(self) -> None:
        app, _host = _app_with_forbidden_runtime()

        result = app.get_runtime_provenance()

        self.assertEqual(result["privacy"]["domAccessCount"], 0)

    def test_runtime_provenance_returns_module_hashes(self) -> None:
        app, _host = _app_with_forbidden_runtime()

        result = app.get_runtime_provenance()

        self.assertIn("playwrightHost", result["moduleHashes"])
        self.assertIn("safetyEngine", result["moduleHashes"])
        self.assertIn("recoveryManager", result["moduleHashes"])
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", value) for value in result["moduleHashes"].values()))
        self.assertTrue(all(result["loadedCodeFingerprints"].values()))

    def test_runtime_provenance_reports_runtime_source_root(self) -> None:
        app, _host = _app_with_forbidden_runtime()

        result = app.get_runtime_provenance()

        expected = str(Path(__file__).resolve().parents[2])
        self.assertEqual(result["projectRoot"], expected)
        self.assertEqual(result["sourceRoot"], expected)

    def test_runtime_provenance_reports_product_shell_pid(self) -> None:
        app, _host = _app_with_forbidden_runtime()

        result = app.get_runtime_provenance()

        self.assertEqual(result["process"]["pid"], os.getpid())
        self.assertTrue(result["process"]["pythonExecutable"])
        self.assertTrue(result["process"]["startedAt"])

    def test_runtime_provenance_has_no_user_content(self) -> None:
        app, _host = _app_with_forbidden_runtime()

        result = app.get_runtime_provenance()
        encoded = json.dumps(result, sort_keys=True)

        self.assertNotIn("fixture-private-message", encoded)
        self.assertNotIn("conversationTitle", encoded)
        self.assertNotIn("assistantMessage", encoded)
        self.assertFalse(result["privacy"]["userContentReturned"])

    def test_runtime_provenance_has_no_auth_secret(self) -> None:
        app, _host = _app_with_forbidden_runtime()

        result = app.get_runtime_provenance()
        forbidden_keys = {"cookie", "token", "password", "authorization", "secret", "storage", "apikey"}
        keys = {key.lower() for key in _all_keys(result)}

        self.assertFalse(keys & forbidden_keys)
        self.assertFalse(result["privacy"]["authSecretReturned"])

    def test_runtime_provenance_is_deterministic_for_same_process(self) -> None:
        app, _host = _app_with_forbidden_runtime()

        first = app.get_runtime_provenance()
        second = app.get_runtime_provenance()

        self.assertEqual(first, second)

    def test_runtime_provenance_changes_pid_after_restart(self) -> None:
        app, _host = _app_with_forbidden_runtime()
        current_pid = app.get_runtime_provenance()["process"]["pid"]
        project_root = Path(__file__).resolve().parents[2]
        script = (
            "from pathlib import Path; "
            "from ai_meeting_room.runtime.identity import RuntimeIdentity; "
            f"print(RuntimeIdentity.create(Path({str(project_root)!r})).process_pid)"
        )

        next_process_pid = int(subprocess.check_output(
            [sys.executable, "-c", script],
            cwd=project_root,
            text=True,
        ).strip())

        self.assertNotEqual(current_pid, next_process_pid)


def _all_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _all_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_keys(child)


class RuntimeProvenanceRouteTests(unittest.TestCase):
    def test_dedicated_provenance_route_does_not_call_mixed_runtime_identity(self) -> None:
        class App:
            def __init__(self):
                self.provenance_calls = 0
                self.identity_calls = 0

            def get_runtime_provenance(self):
                self.provenance_calls += 1
                return {"schemaVersion": "P3A_RUNTIME_PROVENANCE_V1"}

            def runtime_identity(self):
                self.identity_calls += 1
                raise AssertionError("the mixed live-health identity path is prohibited")

        app = App()

        class Handler(ProductHandler):
            def __init__(self):
                self.path = "/api/runtime/provenance"
                self.app = app
                self.client_address = ("127.0.0.1", 12345)
                self.rfile = io.BytesIO()
                self.headers = {}
                self.server = SimpleNamespace()
                self.response = None

            def _json(self, status, payload):
                self.response = (status, payload)

        handler = Handler()
        handler.do_GET()

        self.assertEqual(handler.response, (200, {"schemaVersion": "P3A_RUNTIME_PROVENANCE_V1"}))
        self.assertEqual(app.provenance_calls, 1)
        self.assertEqual(app.identity_calls, 0)


if __name__ == "__main__":
    unittest.main()
