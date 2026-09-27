from __future__ import annotations

import unittest
from pathlib import Path


CONTENT_SCRIPT = (Path(__file__).parents[2] / "browser_extension" / "content_script.js").read_text()


class ChatGPTExtensionCompatibilityTests(unittest.TestCase):
    """Static contract checks for the browser-owned DOM compatibility layer."""

    def test_send_lookup_after_composer_population(self) -> None:
        execute = CONTENT_SCRIPT[CONTENT_SCRIPT.index("async function executeBrainRequest"):]
        self.assertLess(execute.index("writePrompt(composer, request.prompt)"), execute.index("sendControl(composer)"))

    def test_send_button_by_data_testid(self) -> None:
        self.assertIn('[data-testid="send-button"]', CONTENT_SCRIPT)
        self.assertIn("'data-testid'", CONTENT_SCRIPT)

    def test_send_button_by_accessible_role(self) -> None:
        self.assertIn("querySelectorAll('button')", CONTENT_SCRIPT)
        self.assertIn("aria-label", CONTENT_SCRIPT)
        self.assertIn("accessible-role", CONTENT_SCRIPT)

    def test_submit_button_fallback(self) -> None:
        self.assertIn("button[type=\"submit\"]", CONTENT_SCRIPT)
        self.assertIn("form-requestSubmit", CONTENT_SCRIPT)
        self.assertIn("requestSubmit()", CONTENT_SCRIPT)

    def test_enter_fallback_only_when_safe(self) -> None:
        self.assertIn("enterFallbackAllowed", CONTENT_SCRIPT)
        self.assertIn("!node.__aimrComposing", CONTENT_SCRIPT)
        self.assertIn("!this.visibleModal()", CONTENT_SCRIPT)
        self.assertIn("key: 'Enter'", CONTENT_SCRIPT)

    def test_input_events_update_composer_state(self) -> None:
        self.assertIn("inputEvent('beforeinput'", CONTENT_SCRIPT)
        self.assertIn("inputEvent('input'", CONTENT_SCRIPT)
        self.assertIn("HTMLTextAreaElement.prototype", CONTENT_SCRIPT)
        self.assertIn("document.execCommand('insertText'", CONTENT_SCRIPT)

    def test_send_not_confirmed_fails_closed(self) -> None:
        self.assertIn("waitForSendConfirmation", CONTENT_SCRIPT)
        self.assertIn("SEND_NOT_CONFIRMED", CONTENT_SCRIPT)
        self.assertIn("SEND_CONTROL_DISABLED", CONTENT_SCRIPT)

    def test_binding_checked_before_send(self) -> None:
        binding = CONTENT_SCRIPT.index("CONVERSATION_BINDING_MISMATCH")
        send = CONTENT_SCRIPT.index("control.element.click()")
        self.assertLess(binding, send)

    def test_diagnostics_are_semantic_and_response_text_is_not_collected(self) -> None:
        start = CONTENT_SCRIPT.index("diagnostics()")
        end = CONTENT_SCRIPT.index("\n  },\n};", start)
        diagnostics = CONTENT_SCRIPT[start:end]
        self.assertIn("nearbyButtons", diagnostics)
        self.assertIn("metadata(composer)", diagnostics)
        self.assertNotIn("assistant", diagnostics)
        self.assertNotIn("innerText", diagnostics)
        self.assertNotIn("localStorage", diagnostics)
        self.assertNotIn("cookie", diagnostics.lower())


if __name__ == "__main__":
    unittest.main()
