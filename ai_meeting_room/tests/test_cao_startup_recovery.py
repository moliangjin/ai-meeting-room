from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_meeting_room.persistence.sqlite_store import SQLiteStore
from ai_meeting_room.product.app import Phase2Application
from ai_meeting_room.product import serve
from ai_meeting_room.runtime.cao_service_manager import CaoServiceError


class CaoStartupRecoveryWiringTests(unittest.TestCase):
    def test_product_startup_recovers_cao_before_http_server_is_created(self) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = SimpleNamespace(
                database=root / "meeting.db",
                validate_containment=lambda: None,
                ensure=lambda: None,
            )

            class FakeApp:
                local_brain_bridge = object()

                def __init__(self, *_args, **_kwargs) -> None:
                    events.append("app-created")

                def initialize_startup_runtime(self) -> dict[str, str]:
                    events.append("cao-recovery")
                    return {"status": "READY"}

                def close(self) -> None:
                    events.append("app-closed")

            class FakeServer:
                def serve_forever(self) -> None:
                    events.append("serving")
                    raise KeyboardInterrupt

                def server_close(self) -> None:
                    events.append("server-closed")

            logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)
            with (
                patch.object(serve, "resolve_product_paths_from_environment", return_value=SimpleNamespace(database=paths.database)),
                patch.object(serve.ProductDataPaths, "from_database", return_value=paths),
                patch.object(serve, "configure_product_logging", return_value=logger),
                patch.object(serve, "SQLiteStore", return_value=object()),
                patch.object(serve, "Phase2Application", FakeApp),
                patch.object(serve, "make_server", side_effect=lambda *_args, **_kwargs: (events.append("http-server-created") or FakeServer())),
            ):
                result = serve.main(["--no-legacy-extension-bridge"])

        self.assertEqual(result, 0)
        self.assertLess(events.index("cao-recovery"), events.index("http-server-created"))
        self.assertIn("serving", events)

    def test_startup_cao_failure_is_retained_with_safe_diagnostics_and_join_stays_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()

            class FailingCaoManager:
                def recover(self) -> dict[str, object]:
                    raise CaoServiceError("CAO_START_FAILED")

            app = Phase2Application(
                SQLiteStore(root / "meeting.db"),
                cao_service_manager=FailingCaoManager(),
            )
            try:
                with (
                    patch.object(
                        app.runtime_tool_resolver,
                        "resolve_path",
                        return_value=SimpleNamespace(usable=True, path="/opt/homebrew/bin/cao-server"),
                    ),
                    patch.object(app, "runtime_preflight", return_value=SimpleNamespace(cao_server_healthy=False)),
                ):
                    recovery = app.initialize_startup_runtime()
                    without_meeting = next(item for item in app.provider_join_readiness() if item["providerId"] == "codex")
                    meeting_id = app.create_meeting("startup recovery", str(workspace))["meeting"]["meeting_id"]
                    codex = next(item for item in app.provider_join_readiness(meeting_id) if item["providerId"] == "codex")

                self.assertEqual(recovery["status"], "BLOCKED")
                self.assertEqual(recovery["reasonCode"], "CAO_START_FAILED")
                self.assertEqual(recovery["caoServerPath"], "/opt/homebrew/bin/cao-server")
                self.assertEqual(recovery["httpHealthStatus"], "UNAVAILABLE")
                self.assertTrue(recovery["requestId"])
                self.assertEqual(without_meeting["reasonCode"], "CAO_START_FAILED")
                self.assertFalse(without_meeting["joinAvailable"])
                self.assertFalse(codex["joinAvailable"])
                self.assertEqual(codex["reasonCode"], "CAO_START_FAILED")
                self.assertEqual(codex["reason"], "Codex 暂不可用：CAO 服务启动失败。")
                self.assertEqual(codex["caoServerPath"], "/opt/homebrew/bin/cao-server")
                self.assertEqual(codex["httpHealthStatus"], "UNAVAILABLE")
                self.assertEqual(codex["requestId"], recovery["requestId"])
            finally:
                app.close()

    def test_codex_readiness_ui_has_safe_recovery_details_and_refreshes(self) -> None:
        from ai_meeting_room.product.server import UI_HTML

        self.assertIn("CAO_START_FAILED:'Codex 暂不可用：CAO 服务启动失败。'", UI_HTML)
        self.assertIn("HTTP health 状态：", UI_HTML)
        self.assertIn("caoServerPath", UI_HTML)
        self.assertIn("requestId", UI_HTML)
        self.assertIn("refreshCodexJoinReadiness", UI_HTML)


if __name__ == "__main__":
    unittest.main()
