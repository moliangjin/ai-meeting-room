from __future__ import annotations

import unittest

from ai_meeting_room.brain.chatgpt_page_selection import ChatGPTPageFingerprint, ChatGPTPageResolver, ChatGPTPageScorer
from ai_meeting_room.brain.playwright_attached_brain import PlaywrightAttachedBrainHost
from ai_meeting_room.brain.playwright_brain import ChatGPTPlaywrightDomAdapter


def fingerprint(index: int = 0, **overrides):
    value = {
        "candidateIndex": index,
        "pathname": "/",
        "pageTitle": "ChatGPT",
        "isClosed": False,
        "visibilityState": "visible",
        "documentReadyState": "complete",
        "loginUiDetected": False,
        "authenticatedShellDetected": True,
        "composerCandidateCount": 1,
        "visibleComposerCount": 1,
        "editableComposerCount": 1,
    }
    value.update(overrides)
    return value


class FakePage:
    def __init__(self, url: str):
        self.url = url


class FakeLocator:
    def __init__(self, visible: bool = True, editable: bool = True):
        self.visible = visible
        self.editable = editable

    def count(self):
        return 1

    def nth(self, _index):
        return self

    def is_visible(self):
        return self.visible

    def is_editable(self):
        return self.editable


class SelectorOnlyPage:
    def __init__(self, selector: str, *, visible: bool = True, editable: bool = True):
        self.url = "https://chatgpt.com/"
        self.selector = selector
        self.visible = visible
        self.editable = editable

    def evaluate(self, _expression):
        return "complete"

    def locator(self, selector):
        if selector in ChatGPTPlaywrightDomAdapter.APP_SHELL_SELECTORS:
            return FakeLocator()
        if selector == self.selector:
            return FakeLocator(self.visible, self.editable)
        return FakeLocator(visible=False)


class ChatGPTPageSelectionTests(unittest.TestCase):
    def resolve(self, values):
        pages = [FakePage("https://chatgpt.com/c/" + str(index)) for index in range(len(values))]
        resolver = ChatGPTPageResolver()
        selected, diagnostics = resolver.resolve_with_diagnostics(
            pages, lambda _page, index: values[index]
        )
        return pages, selected, diagnostics

    def test_multiple_chatgpt_pages_selects_authenticated_composer_ready_page(self):
        pages, selected, _ = self.resolve([
            fingerprint(0, authenticatedShellDetected=False, loginUiDetected=True, composerCandidateCount=0, visibleComposerCount=0, editableComposerCount=0),
            fingerprint(1),
        ])
        self.assertIs(selected, pages[1])

    def test_first_chatgpt_page_is_not_blindly_selected(self):
        pages, selected, _ = self.resolve([
            fingerprint(0, authenticatedShellDetected=False, composerCandidateCount=0, visibleComposerCount=0, editableComposerCount=0),
            fingerprint(1),
        ])
        self.assertIsNot(selected, pages[0])

    def test_missing_composer_does_not_imply_auth_required(self):
        result = ChatGPTPageScorer.score(ChatGPTPageFingerprint.from_mapping(
            fingerprint(authenticatedShellDetected=True, composerCandidateCount=0, visibleComposerCount=0, editableComposerCount=0)
        ))
        self.assertEqual(result["authState"], "AUTHENTICATED")
        self.assertEqual(result["failureCode"], "COMPOSER_NOT_FOUND")

    def test_explicit_login_ui_maps_auth_required(self):
        result = ChatGPTPageScorer.score(ChatGPTPageFingerprint.from_mapping(
            fingerprint(authenticatedShellDetected=False, loginUiDetected=True, composerCandidateCount=0, visibleComposerCount=0, editableComposerCount=0)
        ))
        self.assertEqual(result["authState"], "AUTH_REQUIRED")
        self.assertEqual(result["failureCode"], "AUTH_REQUIRED")

    def test_authenticated_shell_without_composer_maps_composer_not_found(self):
        result = ChatGPTPageScorer.score(ChatGPTPageFingerprint.from_mapping(
            fingerprint(composerCandidateCount=0, visibleComposerCount=0, editableComposerCount=0)
        ))
        self.assertEqual(result["failureCode"], "COMPOSER_NOT_FOUND")

    def test_contenteditable_composer_detected(self):
        result = ChatGPTPageScorer.score(ChatGPTPageFingerprint.from_mapping(
            fingerprint(composerCandidateCount=1, visibleComposerCount=1, editableComposerCount=1)
        ))
        self.assertTrue(result["ready"])

    def test_role_textbox_composer_detected(self):
        result = ChatGPTPageScorer.score(ChatGPTPageFingerprint.from_mapping(
            fingerprint(composerCandidateCount=1, visibleComposerCount=1, editableComposerCount=1)
        ))
        self.assertEqual(result["category"], "COMPOSER_READY")

    def test_hidden_composer_ignored(self):
        result = ChatGPTPageScorer.score(ChatGPTPageFingerprint.from_mapping(
            fingerprint(composerCandidateCount=1, visibleComposerCount=0, editableComposerCount=0)
        ))
        self.assertEqual(result["failureCode"], "COMPOSER_NOT_FOUND")

    def test_loading_chatgpt_page_not_mapped_auth_required(self):
        result = ChatGPTPageScorer.score(ChatGPTPageFingerprint.from_mapping(
            fingerprint(documentReadyState="interactive", composerCandidateCount=0, visibleComposerCount=0, editableComposerCount=0)
        ))
        self.assertEqual(result["authState"], "LOADING")
        self.assertEqual(result["failureCode"], "PAGE_LOADING")

    def test_three_chatgpt_pages_one_ready_selects_ready_page(self):
        pages, selected, diagnostics = self.resolve([
            fingerprint(0, authenticatedShellDetected=False, loginUiDetected=True, composerCandidateCount=0, visibleComposerCount=0, editableComposerCount=0),
            fingerprint(1, authenticatedShellDetected=True, composerCandidateCount=0, visibleComposerCount=0, editableComposerCount=0),
            fingerprint(2),
        ])
        self.assertIs(selected, pages[2])
        self.assertEqual(sum(item["selectedCandidate"] for item in diagnostics), 1)

    def test_multiple_ready_pages_use_deterministic_policy(self):
        pages, selected, _ = self.resolve([fingerprint(0), fingerprint(1)])
        self.assertIs(selected, pages[0])

    def test_candidate_diagnostics_do_not_read_chat_content(self):
        pages, _, diagnostics = self.resolve([fingerprint(0)])
        self.assertEqual(len(pages), 1)
        self.assertNotIn("text", str(diagnostics).lower())
        self.assertNotIn("prompt", str(diagnostics).lower())
        self.assertNotIn("cookie", str(diagnostics).lower())
        self.assertNotIn("token", str(diagnostics).lower())

    def test_contenteditable_composer_fallback_is_detected(self):
        result = PlaywrightAttachedBrainHost._page_fingerprint(
            SelectorOnlyPage('[contenteditable="true"]'), 0
        )
        self.assertEqual(result["editableComposerCount"], 1)

    def test_role_textbox_composer_fallback_is_detected(self):
        result = PlaywrightAttachedBrainHost._page_fingerprint(
            SelectorOnlyPage('[role="textbox"]'), 0
        )
        self.assertEqual(result["editableComposerCount"], 1)

    def test_hidden_composer_fallback_is_ignored(self):
        result = PlaywrightAttachedBrainHost._page_fingerprint(
            SelectorOnlyPage('[contenteditable="true"]', visible=False), 0
        )
        self.assertEqual(result["visibleComposerCount"], 0)
        self.assertEqual(result["editableComposerCount"], 0)


if __name__ == "__main__":
    unittest.main()
