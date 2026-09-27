from __future__ import annotations

import errno
import inspect
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_meeting_room.brain.dedicated_cdp import (
    DEDICATED_CHROME_USER_DATA_DIR,
    DevToolsActivePortRecord,
    run_product_shell_exact_cdp_diagnostic,
    tcp_connect_probe,
)
from ai_meeting_room.product.app import Phase2Application
from ai_meeting_room.product.server import ProductHandler


class R26ExactCdpDiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = DevToolsActivePortRecord(
            path=DEDICATED_CHROME_USER_DATA_DIR / "DevToolsActivePort",
            port=56128,
            ws_path="/devtools/browser/REDACTED",
        )

    def test_tcp_eperm_classified_as_execution_context_permission(self) -> None:
        error = PermissionError(errno.EPERM, "operation not permitted")
        with patch("ai_meeting_room.brain.dedicated_cdp.socket.create_connection", side_effect=error):
            result = tcp_connect_probe(self.record)
        self.assertEqual(result["result"], "EPERM")
        self.assertEqual(result["errnoName"], "EPERM")
        self.assertEqual(result["syscall"], "connect")

    def test_tcp_refused_not_classified_as_permission(self) -> None:
        error = ConnectionRefusedError(errno.ECONNREFUSED, "connection refused")
        with patch("ai_meeting_room.brain.dedicated_cdp.socket.create_connection", side_effect=error):
            result = tcp_connect_probe(self.record)
        self.assertEqual(result["result"], "ECONNREFUSED")
        self.assertEqual(result["errnoName"], "ECONNREFUSED")
        self.assertNotEqual(result["result"], "EPERM")

    def test_exact_attach_diagnostic_runs_in_product_shell_process(self) -> None:
        source = inspect.getsource(run_product_shell_exact_cdp_diagnostic)
        self.assertIn("os.getpid()", source)
        self.assertIn("connect_over_cdp(record.endpoint", source)
        self.assertIn('"executionContext": "PRODUCT_SHELL"', source)

    def test_codex_environment_result_not_used_as_formal_browser_health(self) -> None:
        source = inspect.getsource(Phase2Application.dev_exact_cdp_diagnostic)
        self.assertIn("run_product_shell_exact_cdp_diagnostic", source)
        self.assertNotIn("tcp_connect_probe", source)

    def test_product_shell_attach_required_for_live_pass(self) -> None:
        source = inspect.getsource(run_product_shell_exact_cdp_diagnostic)
        self.assertIn('result["ok"] = True', source)
        self.assertIn('result["productShellWsAttach"] = "PASS"', source)
        self.assertIn('tcp["result"] != "PASS"', source)

    def test_dev_diagnostic_reads_no_sensitive_browser_data(self) -> None:
        source = inspect.getsource(run_product_shell_exact_cdp_diagnostic)
        self.assertIn('"sensitiveProfileDataRead": False', source)
        for forbidden in ("cookies", "localStorage", "sessionStorage", "loginData", "passwords"):
            self.assertNotIn(forbidden, source.lower())

    def test_exact_diagnostic_route_is_local_dev_only(self) -> None:
        source = inspect.getsource(ProductHandler.do_POST)
        self.assertIn("/api/dev/brain/exact-cdp-diagnostic", source)
        self.assertIn('"127.0.0.1", "::1"', source)
        self.assertIn("dev_probe_enabled", source)


if __name__ == "__main__":
    unittest.main()
