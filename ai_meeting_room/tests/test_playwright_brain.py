from __future__ import annotations

import inspect
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai_meeting_room.brain.playwright_brain import (
    PLAYWRIGHT_BROWSER_RUNTIME,
    ChatGPTPageResolver,
    ChatGPTPlaywrightDomAdapter,
    PlaywrightBrainHost,
    PlaywrightBrainProfile,
    PlaywrightBrainState,
    PlaywrightBrainTransport,
)


class FakeLocator:
    def __init__(self, count: int = 0, *, editable: bool = True, text: str = "") -> None:
        self._count = count
        self._editable = editable
        self._text = text

    def count(self) -> int:
        return self._count

    def nth(self, _index: int) -> "FakeLocator":
        return self

    def last(self) -> "FakeLocator":
        return self

    def is_visible(self) -> bool:
        return True

    def is_editable(self) -> bool:
        return self._editable

    def inner_text(self) -> str:
        return self._text


class FakePage:
    def __init__(self, url: str, *, composer: bool = False, challenge: bool = False, login: bool = False, active: bool = True, ready: str = "complete") -> None:
        self.url = url
        self.composer = composer
        self.challenge = challenge
        self.login = login
        self.active = active
        self.ready = ready

    def locator(self, selector: str) -> FakeLocator:
        if selector in ChatGPTPlaywrightDomAdapter.COMPOSER_SELECTORS:
            return FakeLocator(int(self.composer))
        if selector in ChatGPTPlaywrightDomAdapter.APP_SHELL_SELECTORS:
            return FakeLocator(1)
        if selector in ChatGPTPlaywrightDomAdapter.LOGIN_SELECTORS:
            return FakeLocator(int(self.login))
        if selector == '[data-message-author-role="assistant"]':
            return FakeLocator(0)
        return FakeLocator(0)

    def get_by_text(self, pattern) -> FakeLocator:
        value = getattr(pattern, "pattern", "")
        return FakeLocator(int(self.challenge and "verify" in value.lower()))

    def evaluate(self, expression: str):
        if "visibilityState" in expression:
            return self.active
        if "readyState" in expression:
            return self.ready
        return None


class PlaywrightBrainTests(unittest.TestCase):
    def test_formal_brain_uses_playwright_runtime(self) -> None:
        from ai_meeting_room.product.app import Phase2Application

        source = inspect.getsource(Phase2Application.__init__)
        self.assertNotIn("PlaywrightBrainHost(", source)
        self.assertIn("PlaywrightAttachedBrainHost", inspect.getsource(Phase2Application))
        self.assertEqual(PLAYWRIGHT_BROWSER_RUNTIME, "PLAYWRIGHT_PERSISTENT_BROWSER_RUNTIME")

    def test_raw_cdp_runtime_not_called_by_default(self) -> None:
        from ai_meeting_room.product.app import Phase2Application

        source = inspect.getsource(Phase2Application)
        self.assertNotIn("ChromeWebBrainHost(", source)
        self.assertNotIn("ChromeBrainRuntime(", source)
        self.assertIn("self.real_chrome_brain", source)

    def test_playwright_uses_dedicated_user_data_dir(self) -> None:
        source = inspect.getsource(PlaywrightBrainHost.connect)
        self.assertIn("launch_persistent_context", source)
        self.assertIn("str(self.profile.path)", source)
        self.assertIn('headless=False', source)
        self.assertIn('channel="chrome"', source)

    def test_default_chrome_profile_forbidden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = PlaywrightBrainProfile(Path(directory) / "Default")
            with self.assertRaisesRegex(Exception, "DEFAULT_CHROME_PROFILE_FORBIDDEN"):
                profile.create()

    def test_persistent_context_reused(self) -> None:
        source = inspect.getsource(PlaywrightBrainHost.connect)
        self.assertIn("if self.context is not None", source)
        self.assertIn("self.context.pages", source)
        self.assertNotIn("TemporaryDirectory", source)

    def test_chatgpt_page_created_if_missing(self) -> None:
        source = inspect.getsource(PlaywrightBrainHost.ensure_chatgpt_page)
        self.assertIn("new_page", source)
        self.assertIn("CHATGPT_URL", source)

    def test_auth_required_detection(self) -> None:
        page = FakePage("https://chatgpt.com/", login=True)
        self.assertEqual(ChatGPTPlaywrightDomAdapter.status(page), "AUTH_REQUIRED")

    def test_challenge_required_detection(self) -> None:
        page = FakePage("https://challenges.cloudflare.com/", challenge=True)
        self.assertEqual(ChatGPTPlaywrightDomAdapter.status(page), "CHALLENGE_REQUIRED")

    def test_authenticated_composer_ready(self) -> None:
        page = FakePage("https://chatgpt.com/", composer=True)
        self.assertEqual(ChatGPTPlaywrightDomAdapter.status(page), "AUTHENTICATED")
        self.assertIsNotNone(ChatGPTPlaywrightDomAdapter.find_composer(page))

    def test_page_resolver_ignores_non_chatgpt_pages(self) -> None:
        resolver = ChatGPTPageResolver()
        self.assertIsNone(resolver.resolve([FakePage("https://accounts.google.com/", active=True)]))
        self.assertFalse(resolver.eligible(FakePage("chrome://newtab/")))

    def test_browser_close_fail_closed(self) -> None:
        host = PlaywrightBrainHost()
        host._fail(PlaywrightBrainState.UNKNOWN, "PLAYWRIGHT_BROWSER_LOST")
        self.assertEqual(host.state, PlaywrightBrainState.UNKNOWN)
        self.assertEqual(host.last_error, "PLAYWRIGHT_BROWSER_LOST")

    def test_page_close_fail_closed(self) -> None:
        source = inspect.getsource(PlaywrightBrainHost.auth_status)
        self.assertIn("PLAYWRIGHT_BROWSER_LOST", source)
        self.assertIn("_fail", source)

    def test_recovery_relaunches_same_persistent_context(self) -> None:
        source = inspect.getsource(PlaywrightBrainHost.recover)
        self.assertIn("self.close()", source)
        self.assertIn("self.start()", source)
        self.assertIn("checkHealth", source)
        self.assertIn("self.profile.path", inspect.getsource(PlaywrightBrainHost.connect))

    def test_transport_cannot_mutate_meeting_core(self) -> None:
        source = inspect.getsource(PlaywrightBrainTransport)
        self.assertNotIn("MeetingCore", source)
        self.assertNotIn("SQLiteStore", source)
        self.assertNotIn("BrainInbox", source)

    def test_no_secret_or_storage_export(self) -> None:
        source = inspect.getsource(PlaywrightBrainHost) + inspect.getsource(PlaywrightBrainTransport)
        self.assertNotIn("storage_state", source)
        self.assertNotIn("document.cookie", source)
        self.assertNotIn("Authorization", source)
        self.assertNotIn("refreshToken", source)

    def test_profile_permissions_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = PlaywrightBrainProfile(Path(directory) / "brain").create()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
