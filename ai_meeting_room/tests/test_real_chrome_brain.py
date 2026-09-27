from __future__ import annotations

import inspect
import unittest

from ai_meeting_room.brain.real_chrome_brain import (
    AuthorizedChatGPTTarget,
    ChatGPTTargetPolicy,
    check_chrome_version,
    DEDICATED_REAL_CHROME_PROFILE_NAME,
    MCP_AUTOCONNECT_FLAG,
    MCP_AUTOCONNECT_PACKAGE,
    REAL_CHROME_BROWSER_RUNTIME,
    RealChromeBrainHost,
    RealChromeBrainState,
    RealChromeBrainTransport,
    is_allowed_chatgpt_url,
)


class FakeOfficialConnector:
    def __init__(self, target: AuthorizedChatGPTTarget | None = None, status: str = "AUTHENTICATED") -> None:
        self.target = target
        self.status = status
        self.authorization_requests = 0
        self.detached = 0

    def request_user_authorization(self) -> None:
        self.authorization_requests += 1

    def authorized_chatgpt_target(self):
        return self.target

    def target_status(self, _target):
        return self.status

    def open_conversation(self, target, url):
        self.target = AuthorizedChatGPTTarget(target.target_reference, url, url)
        return self.target

    def send_prompt(self, _target, _prompt):
        return None

    def wait_for_completion(self, _target, _timeout):
        return None

    def read_response(self, _target):
        return '{"decision":"ACCEPT"}'

    def detach(self):
        self.detached += 1


class RealChromeBrainTests(unittest.TestCase):
    def test_daily_chrome_profile_is_not_formal_target(self) -> None:
        from ai_meeting_room.product.app import Phase2Application

        source = inspect.getsource(Phase2Application.__init__)
        self.assertIn("PlaywrightAttachedBrainHost", source)
        self.assertNotIn("Google/Chrome", source)
        self.assertEqual(DEDICATED_REAL_CHROME_PROFILE_NAME, "AI Meeting Room")

    def test_mcp_scope_is_reported_honestly(self) -> None:
        host = RealChromeBrainHost()
        data = host.status()
        self.assertEqual(data["mcpScopeCapability"], "PROFILE_WIDE")
        self.assertEqual(data["applicationTabPolicy"], "CHATGPT_ONLY")
        self.assertEqual(data["applicationCookieUsage"], "FORBIDDEN_BY_POLICY")

    def test_application_policy_allows_chatgpt_only(self) -> None:
        self.assertTrue(ChatGPTTargetPolicy.allows("https://chatgpt.com/"))
        self.assertTrue(ChatGPTTargetPolicy.allows("https://chatgpt.com/g/g-p-123/c/abc"))
        self.assertFalse(ChatGPTTargetPolicy.allows("https://gmail.com/"))

    def test_application_never_reads_cookies(self) -> None:
        source = inspect.getsource(RealChromeBrainHost)
        self.assertNotIn("get_cookies", source)
        self.assertIn('"cookieReadByApp": "NONE"', source)

    def test_application_never_reads_tokens(self) -> None:
        source = inspect.getsource(RealChromeBrainHost)
        self.assertNotIn("storage_state", source)
        self.assertIn('"tokenReadByApp": "NONE"', source)

    def test_application_never_reads_non_chatgpt_tab_content(self) -> None:
        source = inspect.getsource(RealChromeBrainHost)
        self.assertNotIn("list_pages", source)
        self.assertNotIn("context.pages", source)
        self.assertNotIn("page_content", source)

    def test_chrome_version_requirement(self) -> None:
        self.assertEqual(check_chrome_version("144.0.1.2"), "PASS")
        self.assertEqual(check_chrome_version("143.0.0.0"), "FAIL")
        self.assertEqual(check_chrome_version(None), "UNKNOWN")

    def test_mcp_autoconnect_configuration_is_official_boundary(self) -> None:
        self.assertEqual(MCP_AUTOCONNECT_PACKAGE, "chrome-devtools-mcp@latest")
        self.assertEqual(MCP_AUTOCONNECT_FLAG, "--autoConnect")
        source = inspect.getsource(RealChromeBrainHost)
        self.assertNotIn("subprocess", source)

    def test_setup_wizard_is_chinese_and_hides_mcp_command(self) -> None:
        from ai_meeting_room.product import server

        self.assertIn("设置 GPT 主脑", server.UI_HTML)
        self.assertIn("连接 GPT 主脑", server.UI_HTML)
        self.assertIn("chrome://inspect/#remote-debugging", server.UI_HTML)
        self.assertNotIn("npx chrome-devtools-mcp", server.UI_HTML)

    def test_formal_runtime_is_real_chrome_attached(self) -> None:
        from ai_meeting_room.product.app import Phase2Application

        source = inspect.getsource(Phase2Application.__init__)
        self.assertIn("PlaywrightAttachedBrainHost", source)
        self.assertEqual(REAL_CHROME_BROWSER_RUNTIME, "USER_OWNED_REAL_CHROME_SESSION")

    def test_playwright_not_formal_after_google_block(self) -> None:
        from ai_meeting_room.product.app import Phase2Application

        source = inspect.getsource(Phase2Application.__init__)
        self.assertNotIn("PlaywrightBrainHost(", source)
        self.assertIn("PlaywrightAttachedBrainHost(", source)

    def test_only_chatgpt_target_allowed(self) -> None:
        self.assertTrue(is_allowed_chatgpt_url("https://chatgpt.com/"))
        self.assertTrue(is_allowed_chatgpt_url("https://chatgpt.com/c/abc-123"))
        self.assertFalse(is_allowed_chatgpt_url("https://accounts.google.com/"))
        self.assertFalse(is_allowed_chatgpt_url("http://chatgpt.com/"))

    def test_non_chatgpt_target_rejected(self) -> None:
        connector = FakeOfficialConnector(AuthorizedChatGPTTarget("t1", "https://mail.google.com/"))
        host = RealChromeBrainHost(connector=connector)
        with self.assertRaisesRegex(Exception, "NON_CHATGPT_TARGET_REJECTED"):
            host.connect()

    def test_other_tabs_not_read(self) -> None:
        source = inspect.getsource(RealChromeBrainHost)
        self.assertNotIn("list_pages", source)
        self.assertNotIn("context.pages", source)
        self.assertNotIn("enumerate", source)
        self.assertIn("authorized_chatgpt_target", source)

    def test_cookie_access_forbidden(self) -> None:
        source = inspect.getsource(RealChromeBrainHost) + inspect.getsource(RealChromeBrainTransport)
        self.assertNotIn("cookies", source.lower())
        self.assertIn('"cookieAccess": "NONE"', source)

    def test_token_access_forbidden(self) -> None:
        source = inspect.getsource(RealChromeBrainHost) + inspect.getsource(RealChromeBrainTransport)
        self.assertNotIn("storage_state", source)
        self.assertIn('"tokenAccess": "NONE"', source)

    def test_user_authorization_is_explicit(self) -> None:
        connector = FakeOfficialConnector()
        host = RealChromeBrainHost(connector=connector)
        host.connect()
        self.assertEqual(host.state, RealChromeBrainState.WAITING_USER_AUTHORIZATION)
        self.assertEqual(connector.authorization_requests, 1)

    def test_authenticated_target_becomes_ready(self) -> None:
        connector = FakeOfficialConnector(AuthorizedChatGPTTarget("t1", "https://chatgpt.com/c/a"))
        host = RealChromeBrainHost(connector=connector)
        host.connect()
        self.assertEqual(host.state, RealChromeBrainState.READY)
        self.assertEqual(host.auth_status(), "AUTHENTICATED")
        self.assertTrue(host.status()["chatgptTargetBound"])

    def test_bound_tab_close_fail_closed(self) -> None:
        connector = FakeOfficialConnector(AuthorizedChatGPTTarget("t1", "https://chatgpt.com/"), "PAGE_LOST")
        failures = []
        host = RealChromeBrainHost(connector=connector, on_failure=lambda state, reason: failures.append((state, reason)))
        host.connect()
        self.assertFalse(host.checkHealth())
        self.assertEqual(host.state, RealChromeBrainState.PAGE_LOST)
        self.assertTrue(failures)

    def test_mcp_disconnect_fail_closed(self) -> None:
        class Disconnected(FakeOfficialConnector):
            def target_status(self, _target):
                raise RuntimeError("connector disconnected")

        connector = Disconnected(AuthorizedChatGPTTarget("t1", "https://chatgpt.com/"))
        host = RealChromeBrainHost(connector=connector)
        with self.assertRaisesRegex(Exception, "REAL_CHROME_CONNECTOR_ERROR"):
            host.connect()
        self.assertFalse(host.checkHealth())
        self.assertEqual(host.state, RealChromeBrainState.PAGE_LOST)

    def test_navigation_away_fail_closed(self) -> None:
        connector = FakeOfficialConnector(AuthorizedChatGPTTarget("t1", "https://chatgpt.com/"))
        host = RealChromeBrainHost(connector=connector)
        host.connect()
        connector.target = AuthorizedChatGPTTarget("t1", "https://example.com/")
        self.assertEqual(host.auth_status(), "NAVIGATION_AWAY_FROM_CHATGPT")
        self.assertEqual(host.state, RealChromeBrainState.ERROR)

    def test_transport_cannot_mutate_core(self) -> None:
        source = inspect.getsource(RealChromeBrainTransport)
        self.assertNotIn("MeetingCore", source)
        self.assertNotIn("SQLiteStore", source)
        self.assertNotIn("BrainInbox", source)

    def test_conversation_binding_stays_on_chatgpt(self) -> None:
        connector = FakeOfficialConnector(AuthorizedChatGPTTarget("t1", "https://chatgpt.com/"))
        host = RealChromeBrainHost(connector=connector)
        host.connect()
        self.assertTrue(host.open_conversation("conversation-1").startswith("https://chatgpt.com/c/"))
        with self.assertRaisesRegex(Exception, "INVALID_CONVERSATION_BINDING"):
            host.open_conversation("../../other")

    def test_detach_does_not_close_user_chrome(self) -> None:
        connector = FakeOfficialConnector(AuthorizedChatGPTTarget("t1", "https://chatgpt.com/"))
        host = RealChromeBrainHost(connector=connector)
        host.connect()
        host.close()
        self.assertEqual(connector.detached, 1)
        source = inspect.getsource(RealChromeBrainHost.close)
        self.assertNotIn("terminate", source)
        self.assertNotIn("kill(", source)


if __name__ == "__main__":
    unittest.main()
