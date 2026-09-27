from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_meeting_room.agents.adapter import AgentAdapter
from ai_meeting_room.persistence.sqlite_store import SQLiteStore
from ai_meeting_room.product.app import Phase2Application, ProductError
from ai_meeting_room.product.server import UI_HTML, make_server
from ai_meeting_room.models import AgentHealth
from phase0_poc.cao_bridge import CaoHttpError


class JoinTestAdapter(AgentAdapter):
    def __init__(self, terminal: str = "test-terminal") -> None:
        self.agent_id = terminal
        self.session_id = terminal
        self.terminal_id = terminal

    def start(self) -> str: return self.agent_id
    def stop(self) -> None: pass
    def pause(self) -> None: pass
    def resume(self) -> None: pass
    def interrupt(self) -> None: pass
    def send_task(self, task): return str(task["task_id"])
    def get_status(self) -> str: return "IDLE"
    def get_health(self) -> str: return AgentHealth.HEALTHY.value
    def get_output(self) -> str: return ""
    def health_check(self) -> bool: return True


class CodexJoinHotfixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.database = self.root / "meeting.db"
        self.apps: list[Phase2Application] = []

    def tearDown(self) -> None:
        for app in reversed(self.apps):
            app.close()
        self.temp.cleanup()

    def make_app(self, adapter_factory=None) -> Phase2Application:
        app = Phase2Application(
            SQLiteStore(self.database),
            adapter_factory=adapter_factory or (lambda **_: JoinTestAdapter()),
        )
        self.apps.append(app)
        return app

    def make_meeting(self, app: Phase2Application) -> str:
        return app.create_meeting("R81 join test", str(self.workspace))["meeting"]["meeting_id"]

    def test_codex_join_button_sends_request(self) -> None:
        self.assertIn("api('/api/meetings/'+selected+'/agents',{method:'POST',body:JSON.stringify({provider:id})})", UI_HTML)

    def test_codex_join_created_meeting_allowed(self) -> None:
        app = self.make_app()
        meeting_id = self.make_meeting(app)
        result = app.join_provider(meeting_id, "codex")
        self.assertEqual(result["meeting"]["status"], "CREATED")

    def test_codex_join_creates_participant(self) -> None:
        app = self.make_app()
        meeting_id = self.make_meeting(app)
        result = app.join_provider(meeting_id, "codex")
        self.assertEqual(len(result["agents"]), 1)
        self.assertEqual(result["agents"][0]["provider"], "codex")

    def test_codex_join_persists_participant(self) -> None:
        app = self.make_app()
        meeting_id = self.make_meeting(app)
        app.join_provider(meeting_id, "codex")
        app.close()
        self.apps.remove(app)
        restored = self.make_app()
        snapshot = restored.snapshot(meeting_id)
        self.assertEqual(len(snapshot["agents"]), 1)
        self.assertEqual(snapshot["agents"][0]["provider"], "codex")

    def test_codex_join_ui_refreshes(self) -> None:
        self.assertIn("await refresh();await renderProviders()", UI_HTML)
        self.assertIn("joinAvailable", UI_HTML)

    def test_codex_join_duplicate_click_idempotent(self) -> None:
        app = self.make_app()
        meeting_id = self.make_meeting(app)
        creates: list[str] = []
        app._adapter_factory = lambda **kwargs: (creates.append(kwargs["session_id"]) or JoinTestAdapter(kwargs["session_id"]))
        first = app.join_provider(meeting_id, "codex")
        second = app.join_provider(meeting_id, "codex")
        self.assertEqual(len(creates), 1)
        self.assertEqual(len(second["agents"]), 1)
        self.assertEqual(first["agents"][0]["agent_id"], second["agents"][0]["agent_id"])

    def test_codex_join_concurrent_duplicate_requests_create_one_session(self) -> None:
        app = self.make_app()
        meeting_id = self.make_meeting(app)
        creates: list[str] = []
        app._adapter_factory = lambda **kwargs: (creates.append(kwargs["session_id"]) or JoinTestAdapter(kwargs["session_id"]))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: app.join_provider(meeting_id, "codex"), range(2)))
        self.assertEqual(len(creates), 1)
        self.assertEqual(len(results[0]["agents"]), 1)
        self.assertEqual(results[0]["agents"][0]["agent_id"], results[1]["agents"][0]["agent_id"])

    def test_codex_join_cao_unavailable_chinese_error(self) -> None:
        app = self.make_app()
        meeting_id = self.make_meeting(app)
        app._uses_real_cao = True
        app.runtime_preflight = lambda: SimpleNamespace(
            usable=False, reason="CAO_SERVER_UNHEALTHY", cao_server_healthy=False,
            tools={"cao": SimpleNamespace(usable=True), "tmux": SimpleNamespace(usable=True), "codex": SimpleNamespace(usable=True)},
        )
        with self.assertRaises(ProductError) as raised:
            app.join_provider(meeting_id, "codex")
        self.assertEqual(raised.exception.code, "CAO_UNAVAILABLE")
        self.assertIn("Codex 加入会议失败：CAO 服务不可用。", str(raised.exception))

    def test_codex_join_auth_failure_chinese_error(self) -> None:
        app = self.make_app()
        meeting_id = self.make_meeting(app)
        app._uses_real_cao = True
        app.runtime_preflight = lambda: SimpleNamespace(
            usable=True, reason=None, cao_server_healthy=True,
            tools={"cao": SimpleNamespace(usable=True), "tmux": SimpleNamespace(usable=True), "codex": SimpleNamespace(usable=True, path="/fake/codex")},
        )
        with patch.object(app, "_codex_authentication_status", return_value=("BLOCKED", "CODEX_AUTH_REQUIRED"), create=True):
            with self.assertRaises(ProductError) as raised:
                app.join_provider(meeting_id, "codex")
        self.assertEqual(raised.exception.code, "CODEX_AUTH_REQUIRED")
        self.assertIn("Codex 加入会议失败：Codex 登录状态无效。", str(raised.exception))

    def test_codex_join_runtime_failure_chinese_error(self) -> None:
        app = self.make_app(adapter_factory=lambda **_: (_ for _ in ()).throw(
            CaoHttpError("private CAO detail must not escape", status_code=503, error_code="CAO_INTERNAL")
        ))
        meeting_id = self.make_meeting(app)
        with self.assertRaises(ProductError) as raised:
            app.join_provider(meeting_id, "codex")
        self.assertEqual(raised.exception.code, "CODEX_RUNTIME_SESSION_CREATE_FAILED")
        self.assertIn("Codex 加入会议失败：无法创建运行时会话。", str(raised.exception))
        self.assertNotIn("private CAO detail", str(raised.exception))

    def test_codex_join_requires_existing_healthy_gpt_brain(self) -> None:
        app = self.make_app()
        meeting_id = self.make_meeting(app)
        app._brains.pop(meeting_id)
        with self.assertRaises(ProductError) as raised:
            app.join_provider(meeting_id, "codex")
        self.assertEqual(raised.exception.code, "GPT_BRAIN_UNAVAILABLE")
        self.assertFalse(app._core(meeting_id).registry.all())

    def test_codex_ready_badge_matches_join_readiness(self) -> None:
        self.assertIn("Codex 运行环境：", UI_HTML)
        self.assertIn("加入会议：", UI_HTML)
        self.assertIn("joinAvailable", UI_HTML)
        self.assertIn("function renderCodexProviderCard()", UI_HTML)
        self.assertIn("provider_join_readiness(meeting_id)", Path(__file__).resolve().parents[1].joinpath("product", "server.py").read_text())

    def test_generic_operation_failed_not_used_for_codex_join(self) -> None:
        join = UI_HTML.rsplit("async function joinProvider", 1)[1].split("async function selectMeeting", 1)[0]
        self.assertIn("showCodexJoinError(error)", join)
        self.assertNotIn("showLocalizedAlert(error.message)", join.split("if(codexJoinInFlight)", 1)[1])
        self.assertIn('id="codex-join-error"', UI_HTML)
        self.assertIn("技术详情", UI_HTML)

    def test_codex_readiness_requires_live_cao_auth_model_and_workspace(self) -> None:
        app = self.make_app()
        meeting_id = self.make_meeting(app)
        app._uses_real_cao = True
        app.runtime_preflight = lambda: SimpleNamespace(
            usable=True, reason=None, cao_server_healthy=True,
            tools={name: SimpleNamespace(usable=True, path="/fake/codex" if name == "codex" else "/fake/" + name)
                   for name in ("cao", "tmux", "codex")},
        )
        with patch.object(app, "_codex_authentication_status", return_value=("READY", "CODEX_AUTHENTICATED")), \
                patch("ai_meeting_room.product.app.resolve_codex_model", return_value="gpt-5.6-sol"):
            item = next(p for p in app.provider_join_readiness(meeting_id) if p["providerId"] == "codex")
        self.assertEqual(item["runtimeStatus"], "READY")
        self.assertTrue(item["joinAvailable"])
        self.assertEqual(item["joinStatus"], "AVAILABLE")

    def test_codex_joined_readiness_remains_joined_after_refresh(self) -> None:
        app = self.make_app()
        meeting_id = self.make_meeting(app)
        app._uses_real_cao = True
        app.runtime_preflight = lambda: SimpleNamespace(
            usable=True, reason=None, cao_server_healthy=True,
            tools={name: SimpleNamespace(usable=True, path="/fake/codex" if name == "codex" else "/fake/" + name)
                   for name in ("cao", "tmux", "codex")},
        )
        with patch.object(app, "_codex_authentication_status", return_value=("READY", "CODEX_AUTHENTICATED")), \
                patch("ai_meeting_room.product.app.resolve_codex_model", return_value="gpt-5.6-sol"):
            app.join_provider(meeting_id, "codex")
            item = next(p for p in app.provider_join_readiness(meeting_id) if p["providerId"] == "codex")
        self.assertFalse(item["joinAvailable"])
        self.assertEqual(item["joinStatus"], "JOINED")
        self.assertEqual(item["reasonCode"], "ALREADY_JOINED")

    def test_codex_readiness_blocks_join_when_cao_is_unavailable(self) -> None:
        app = self.make_app()
        meeting_id = self.make_meeting(app)
        app._uses_real_cao = True
        app.runtime_preflight = lambda: SimpleNamespace(
            usable=False, reason="CAO_SERVER_UNHEALTHY", cao_server_healthy=False, tools={},
        )
        item = next(p for p in app.provider_join_readiness(meeting_id) if p["providerId"] == "codex")
        self.assertEqual(item["runtimeStatus"], "BLOCKED")
        self.assertFalse(item["joinAvailable"])
        self.assertEqual(item["reasonCode"], "CAO_UNAVAILABLE")

    def test_codex_readiness_blocks_join_when_gpt_brain_is_unavailable(self) -> None:
        app = self.make_app()
        meeting_id = self.make_meeting(app)
        app._brains.pop(meeting_id)
        item = next(p for p in app.provider_join_readiness(meeting_id) if p["providerId"] == "codex")
        self.assertFalse(item["joinAvailable"])
        self.assertEqual(item["reasonCode"], "GPT_BRAIN_UNAVAILABLE")

    def test_codex_join_api_preserves_error_http_and_request_id(self) -> None:
        app = self.make_app()
        server = make_server(app, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(app, "join_provider", side_effect=ProductError(
                "Codex 加入会议失败：CAO 服务不可用。", code="CAO_UNAVAILABLE", stage="CAO_PREFLIGHT",
            )):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/meetings/test/agents",
                    data=json.dumps({"provider": "codex"}).encode(),
                    headers={"Content-Type": "application/json", "X-Request-ID": "r81-test-request"},
                    method="POST",
                )
                try:
                    urllib.request.urlopen(req, timeout=3)
                    self.fail("expected join rejection")
                except urllib.error.HTTPError as error:
                    body = json.loads(error.read())
                    self.assertEqual(error.code, 409)
            self.assertEqual(body["error"]["code"], "CAO_UNAVAILABLE")
            self.assertEqual(body["error"]["stage"], "CAO_PREFLIGHT")
            self.assertEqual(body["error"]["requestId"], "r81-test-request")
            self.assertEqual(body["error"]["httpStatus"], 409)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_codex_join_restored_runtime_allows_start_after_restart(self) -> None:
        app = self.make_app()
        meeting_id = self.make_meeting(app)
        agent_id = app.join_provider(meeting_id, "codex")["agents"][0]["agent_id"]
        app.close()
        self.apps.remove(app)
        restored = self.make_app(adapter_factory=lambda **kwargs: JoinTestAdapter(kwargs["session_id"]))
        result = restored.start_meeting(meeting_id)
        self.assertEqual(result["meeting"]["status"], "RUNNING")
        self.assertEqual(len(result["agents"]), 1)
        self.assertEqual(result["agents"][0]["agent_id"], agent_id)
        self.assertEqual(restored._core(meeting_id).runtime.current_binding(agent_id).generation, 2)


if __name__ == "__main__":
    unittest.main()
