from __future__ import annotations

import inspect
from types import SimpleNamespace
import unittest
from pathlib import Path

from ai_meeting_room.brain.playwright_attached_brain import (
    PlaywrightAttachedBrainError,
    PlaywrightAttachedBrainHost,
    PlaywrightAttachedBrainState,
)
from ai_meeting_room.brain.playwright_brain import ChatGPTPlaywrightDomAdapter
from ai_meeting_room.brain.playwright_brain import ComposerTarget
from ai_meeting_room.brain.chatgpt_web import BrainRequest


class ComposerLocator:
    def __init__(self, *, kind: str = "CONTENTEDITABLE", fill_error: Exception | None = None) -> None:
        self.kind = kind
        self.fill_error = fill_error
        self.value = ""
        self.pressed: list[tuple[str, str | None]] = []

    def count(self) -> int:
        return 1

    def nth(self, _index: int) -> "ComposerLocator":
        return self

    def is_visible(self) -> bool:
        return True

    def is_editable(self) -> bool:
        return True

    def input_value(self) -> str:
        if self.kind == "CONTENTEDITABLE":
            raise RuntimeError("not an input")
        return self.value

    def inner_text(self) -> str:
        return self.value

    def evaluate(self, _expression: str) -> dict[str, object]:
        tag = "TEXTAREA" if self.kind == "TEXTAREA" else "DIV"
        return {
            "tagName": tag,
            "role": "textbox",
            "contenteditable": self.kind in {"CONTENTEDITABLE", "PROSEMIRROR"},
            "dataTestId": "prompt-textarea" if self.kind == "TEXTAREA" else None,
            "classTokens": ["ProseMirror"] if self.kind == "PROSEMIRROR" else [],
        }

    def fill(self, value: str) -> None:
        if self.fill_error is not None:
            raise self.fill_error
        self.value = value

    def focus(self) -> None:
        self.pressed.append(("focus", None))

    def press(self, key: str) -> None:
        self.pressed.append((key, None))
        if key == "ControlOrMeta+A":
            self.value = ""

    def press_sequentially(self, value: str) -> None:
        self.pressed.append(("press_sequentially", value))
        self.value = value


class ComposerPage:
    def __init__(self, locator: ComposerLocator) -> None:
        self.url = "https://chatgpt.com/c/fixture"
        self.locator_value = locator
        self.closed = False

    def locator(self, selector: str) -> ComposerLocator:
        if selector in ChatGPTPlaywrightDomAdapter.COMPOSER_SELECTORS:
            return self.locator_value
        return ComposerLocator()

    def evaluate(self, expression: str) -> dict[str, object] | str:
        if "loginUiDetected" in expression:
            return {
                "visibilityState": "visible",
                "documentReadyState": "complete",
                "loginUiDetected": False,
                "authenticatedShellDetected": True,
                "composerCandidateCount": 1,
                "visibleComposerCount": 1,
                "editableComposerCount": 1,
            }
        if "visibilityState" in expression:
            return "visible"
        if "readyState" in expression:
            return "complete"
        return None

    def is_closed(self) -> bool:
        return self.closed


class ComposerTransactionTests(unittest.TestCase):
    def test_detect_and_write_use_same_composer_resolver(self) -> None:
        locator = ComposerLocator(kind="TEXTAREA")
        target = ChatGPTPlaywrightDomAdapter.resolve_composer(ComposerPage(locator))
        self.assertIsNotNone(target)
        self.assertIs(target.locator, locator)
        self.assertEqual(target.element_kind, "TEXTAREA")

    def test_contenteditable_fill_uses_safe_fallback_when_fill_fails(self) -> None:
        locator = ComposerLocator(kind="PROSEMIRROR", fill_error=RuntimeError("fill unsupported"))
        target = ChatGPTPlaywrightDomAdapter.resolve_composer(ComposerPage(locator))
        self.assertIsNotNone(target)
        ChatGPTPlaywrightDomAdapter.write_composer(target, "hello")
        self.assertEqual(locator.inner_text(), "hello")
        self.assertIn(("press_sequentially", "hello"), locator.pressed)

    def test_write_verification_required(self) -> None:
        locator = ComposerLocator(kind="TEXTAREA")
        target = ChatGPTPlaywrightDomAdapter.resolve_composer(ComposerPage(locator))
        self.assertIsNotNone(target)
        ChatGPTPlaywrightDomAdapter.write_composer(target, "hello")
        self.assertEqual(ChatGPTPlaywrightDomAdapter.composer_text(target), "hello")

    def test_composer_must_be_visible(self) -> None:
        locator = ComposerLocator(kind="TEXTAREA")
        target = ComposerTarget("fixture", locator, "TEXTAREA", False, True, True, 1)
        with self.assertRaisesRegex(RuntimeError, "COMPOSER_NOT_VISIBLE"):
            ChatGPTPlaywrightDomAdapter.write_composer(target, "hello")

    def test_composer_must_be_editable(self) -> None:
        locator = ComposerLocator(kind="TEXTAREA")
        target = ComposerTarget("fixture", locator, "TEXTAREA", True, False, True, 1)
        with self.assertRaisesRegex(RuntimeError, "COMPOSER_NOT_EDITABLE"):
            ChatGPTPlaywrightDomAdapter.write_composer(target, "hello")

    def test_composer_ambiguous_has_explicit_error(self) -> None:
        locator = ComposerLocator(kind="TEXTAREA")
        target = ComposerTarget("fixture", locator, "TEXTAREA", True, True, True, 2)
        with self.assertRaisesRegex(RuntimeError, "COMPOSER_AMBIGUOUS"):
            ChatGPTPlaywrightDomAdapter.write_composer(target, "hello")

    def test_write_failure_does_not_change_auth_state(self) -> None:
        locator = ComposerLocator(kind="TEXTAREA", fill_error=RuntimeError("fill failed"))
        page = ComposerPage(locator)
        host = PlaywrightAttachedBrainHost(playwright_factory=lambda: object())
        host.page = page
        host.context = SimpleNamespace(pages=[page])
        host.browser = SimpleNamespace(contexts=[host.context])
        host.state = PlaywrightAttachedBrainState.READY
        host.auth_state = "AUTHENTICATED"
        host.bound_page_id = "page-0"
        host.selected_candidate_index = 0
        request = BrainRequest(
            brain_request_id="request-1",
            meeting_id="meeting-1",
            task_id="desktop-brain-poc",
            prompt="hello",
            allowed_actions=("ACCEPT",),
            created_at="2026-01-01T00:00:00+00:00",
        )
        host.prepare_request(request)
        with self.assertRaises(PlaywrightAttachedBrainError) as caught:
            host.send_prompt("hello")
        self.assertEqual(caught.exception.code, "COMPOSER_WRITE_FAILED")
        self.assertEqual(host.state, PlaywrightAttachedBrainState.READY)
        self.assertEqual(host.auth_state, "AUTHENTICATED")

    def test_poc_bound_page_cannot_change_between_precheck_and_write(self) -> None:
        locator = ComposerLocator(kind="TEXTAREA")
        page = ComposerPage(locator)
        other_page = ComposerPage(ComposerLocator(kind="TEXTAREA"))
        host = PlaywrightAttachedBrainHost(playwright_factory=lambda: object())
        host.page = page
        host.context = SimpleNamespace(pages=[page, other_page])
        host.browser = SimpleNamespace(contexts=[host.context])
        host.state = PlaywrightAttachedBrainState.READY
        host.auth_state = "AUTHENTICATED"
        host.bound_page_id = "page-0"
        host.selected_candidate_index = 0
        request = BrainRequest(
            brain_request_id="request-2",
            meeting_id="meeting-1",
            task_id="desktop-brain-poc",
            prompt="hello",
            allowed_actions=("ACCEPT",),
            created_at="2026-01-01T00:00:00+00:00",
        )
        host.prepare_request(request)
        host.page = other_page
        with self.assertRaises(PlaywrightAttachedBrainError) as caught:
            host.send_prompt("hello")
        self.assertEqual(caught.exception.code, "POC_BOUND_PAGE_CHANGED")

    def test_authenticated_shell_does_not_treat_submit_button_as_login(self) -> None:
        from ai_meeting_room.brain import playwright_attached_brain as module

        self.assertNotIn("'button[type=\"submit\"]'", module._PAGE_FINGERPRINT_SCRIPT)

    def test_poc_failure_does_not_remap_to_auth_required(self) -> None:
        from ai_meeting_room.brain import playwright_attached_brain as module

        source = inspect.getsource(module.PlaywrightAttachedBrainHost.send_prompt)
        self.assertNotIn("self.auth_status()", source)

    def test_composer_write_failure_not_followed_by_not_ready_error(self) -> None:
        source = Path("desktop/main.js").read_text(encoding="utf-8")
        self.assertNotIn("message: 'GPT Web Brain is not ready'", source)

    def test_health_polling_does_not_concurrently_touch_page(self) -> None:
        source = Path("ai_meeting_room/product/server.py").read_text(encoding="utf-8")
        self.assertIn("HTTPServer", source)
        self.assertNotIn("ThreadingHTTPServer", source)

    def test_playwright_commands_serialized_on_owner_thread(self) -> None:
        source = Path("ai_meeting_room/brain/playwright_attached_brain.py").read_text(encoding="utf-8")
        self.assertIn('_record_owner_thread("composer_write")', source)
        self.assertIn('_record_owner_thread("poc_precheck")', source)


if __name__ == "__main__":
    unittest.main()
