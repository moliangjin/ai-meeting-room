from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from ai_meeting_room.persistence.sqlite_store import SQLiteStore
from ai_meeting_room.product.app import Phase2Application
from ai_meeting_room.product.server import UI_HTML, make_server


class CreateMeetingUiContractTests(unittest.TestCase):
    def test_create_meeting_button_has_handler(self) -> None:
        self.assertIn('id="createMeetingButton"', UI_HTML)
        self.assertIn('onclick="showCreateMeetingDialog()"', UI_HTML)

    def test_create_meeting_frontend_request_sent(self) -> None:
        self.assertIn('id="createMeetingForm"', UI_HTML)
        self.assertIn('submitCreateMeeting(event)', UI_HTML)
        self.assertIn("api('/api/meetings',{method:'POST'", UI_HTML)
        self.assertNotIn('async function createMeeting(){let name=prompt(', UI_HTML)

    def test_packaged_product_ui_does_not_use_unsupported_prompt_dialogs(self) -> None:
        self.assertNotIn("prompt(", UI_HTML)
        self.assertIn("id=\"restoreProductDialog\"", UI_HTML)
        self.assertIn("用户从产品界面请求暂停", UI_HTML)

    def test_product_messages_use_in_app_chinese_dialogs(self) -> None:
        self.assertIn('id="productMessageDialog"', UI_HTML)
        self.assertIn('id="productConfirmDialog"', UI_HTML)
        self.assertIn("function showLocalizedAlert(message)", UI_HTML)
        self.assertIn("function confirmInChinese(message)", UI_HTML)
        self.assertNotRegex(UI_HTML, r"\balert\s*\(")
        self.assertNotRegex(UI_HTML, r"\b(?:window\.)?confirm\s*\(")

    def test_meeting_completion_requires_localized_confirmation(self) -> None:
        self.assertIn("confirmInChinese('确认结束整个会议？这与接受当前任务不同。')", UI_HTML)
        self.assertIn("confirmInChinese(confirmText)", UI_HTML)
        self.assertIn('value="cancel">取消', UI_HTML)
        self.assertIn('value="confirm">确认', UI_HTML)

    def test_create_meeting_appears_in_ui(self) -> None:
        self.assertIn("renderMeetings(await api('/api/meetings'))", UI_HTML)
        self.assertIn("selected=created.meeting.meeting_id", UI_HTML)
        self.assertIn("await refresh()", UI_HTML)

    def test_create_meeting_refreshes_provider_join_actions(self) -> None:
        submit = next(line for line in UI_HTML.splitlines() if "async function submitCreateMeeting(event)" in line)
        self.assertIn("selected=created.meeting.meeting_id", submit)
        self.assertIn("await refresh();await renderProviders()", submit)

    def test_create_meeting_failure_shows_chinese_error(self) -> None:
        self.assertIn('id="createMeetingError"', UI_HTML)
        self.assertIn("MEETING_NAME_REQUIRED", UI_HTML)
        self.assertIn("WORKSPACE_DIRECTORY_INVALID", UI_HTML)
        self.assertIn("DATABASE_UNAVAILABLE", UI_HTML)
        self.assertIn("创建会议失败", UI_HTML)

    def test_empty_name_validation_uses_the_product_chinese_error_ui(self) -> None:
        self.assertRegex(
            UI_HTML,
            r'<form id="createMeetingForm" novalidate onsubmit="submitCreateMeeting\(event\)">',
        )
        self.assertIn(
            "errorBox.textContent=I18N.errors.MEETING_NAME_REQUIRED",
            UI_HTML,
        )

    def test_runtime_diagnostics_use_chinese_safe_descriptions(self) -> None:
        self.assertIn("v101PreflightDiagnosticDetails", UI_HTML)
        self.assertIn("PRODUCT_SHELL_READY:'本地服务正在提供界面'", UI_HTML)
        self.assertIn("DATABASE_READY:'SQLite 本地持久化可用'", UI_HTML)
        self.assertIn("技术错误码：", UI_HTML)

    def test_task_title_control_wraps_long_chinese_text(self) -> None:
        self.assertIn('<textarea id="taskTitle" rows="2"', UI_HTML)
        self.assertNotIn('<input id="taskTitle"', UI_HTML)

    def test_primary_brain_summary_does_not_expose_internal_participant_id(self) -> None:
        self.assertIn("phase3cParticipant=function(s)", UI_HTML)
        self.assertIn("return p.participantId?'GPT 主脑已连接'", UI_HTML)

    def test_create_meeting_double_click_does_not_duplicate(self) -> None:
        self.assertIn('createMeetingInFlight', UI_HTML)
        self.assertIn("submit.disabled=true", UI_HTML)


class CreateMeetingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.database = self.root / "meeting.db"
        self.app = Phase2Application(SQLiteStore(self.database))
        self.server = make_server(self.app, host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.app.close()
        self.temp.cleanup()

    def post_meeting(self, payload: dict[str, str]) -> tuple[int, dict[str, object]]:
        request = urllib.request.Request(
            f"{self.base}/api/meetings",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_create_meeting_api_success(self) -> None:
        status, body = self.post_meeting({"name": "测试会议", "workspacePath": str(self.workspace)})
        self.assertEqual(status, 201)
        self.assertEqual(body["meeting"]["name"], "测试会议")
        self.assertTrue(body["meeting"]["meeting_id"])

    def test_create_meeting_persisted(self) -> None:
        _status, body = self.post_meeting({"name": "持久化会议", "workspacePath": str(self.workspace)})
        meeting_id = body["meeting"]["meeting_id"]
        self.app.close()
        restored = Phase2Application(SQLiteStore(self.database))
        try:
            matches = [item for item in restored.list_meetings() if item["meeting_id"] == meeting_id]
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["name"], "持久化会议")
        finally:
            restored.close()

    def test_create_meeting_rejects_blank_name_with_stable_error_code(self) -> None:
        status, body = self.post_meeting({"name": "   ", "workspacePath": str(self.workspace)})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "MEETING_NAME_REQUIRED")

    def test_create_meeting_rejects_invalid_workspace_with_stable_error_code(self) -> None:
        status, body = self.post_meeting({"name": "无效工作区", "workspacePath": str(self.root / "missing")})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "WORKSPACE_DIRECTORY_INVALID")

    def test_create_meeting_database_failure_has_stable_error_code(self) -> None:
        with patch.object(self.app.store, "save_meeting", side_effect=sqlite3.OperationalError("disk unavailable")):
            status, body = self.post_meeting({"name": "数据库故障", "workspacePath": str(self.workspace)})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "DATABASE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
