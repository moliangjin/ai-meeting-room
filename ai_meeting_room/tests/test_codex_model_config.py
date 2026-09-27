import os
import unittest
from unittest.mock import patch

from ai_meeting_room.integrations.cao.model_config import (
    DEFAULT_CODEX_MODEL,
    CodexModelConfigurationError,
    resolve_codex_model,
)
from ai_meeting_room.product.app import Phase2Application, ProviderBlockedError


class CodexModelConfigurationTests(unittest.TestCase):
    def test_product_owned_default_is_gpt_56_sol(self) -> None:
        self.assertEqual(resolve_codex_model({}), "gpt-5.6-sol")
        self.assertEqual(DEFAULT_CODEX_MODEL, "gpt-5.6-sol")

    def test_product_model_override_survives_finder_env(self) -> None:
        finder_environment = {
            "HOME": "/Users/test",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "AI_MEETING_ROOM_CODEX_MODEL": "gpt-5.6-sol",
        }
        self.assertEqual(resolve_codex_model(finder_environment), "gpt-5.6-sol")

    def test_unsupported_model_override_fails_closed(self) -> None:
        with self.assertRaises(CodexModelConfigurationError) as raised:
            resolve_codex_model({"AI_MEETING_ROOM_CODEX_MODEL": "gpt-6-luna"})
        self.assertEqual(raised.exception.code, "CODEX_MODEL_UNSUPPORTED_FOR_AUTH")
        self.assertIn("gpt-5.6-sol", str(raised.exception))

    def test_product_passes_explicit_model_to_cao_adapter(self) -> None:
        app = Phase2Application.__new__(Phase2Application)
        app.cao_base_url = "http://127.0.0.1:9889"
        with patch.dict(os.environ, {}, clear=True), patch(
            "ai_meeting_room.product.app.CaoAgentAdapter.create", return_value=object()
        ) as create:
            app._create_cao_adapter(provider="codex", session_id="test", working_directory="/tmp")
        self.assertEqual(create.call_args.kwargs["model"], "gpt-5.6-sol")

    def test_unsupported_product_model_has_human_readable_block(self) -> None:
        app = Phase2Application.__new__(Phase2Application)
        app.cao_base_url = "http://127.0.0.1:9889"
        with patch.dict(os.environ, {"AI_MEETING_ROOM_CODEX_MODEL": "gpt-6-luna"}):
            with self.assertRaises(ProviderBlockedError) as raised:
                app._create_cao_adapter(provider="codex", session_id="test", working_directory="/tmp")
        self.assertEqual(raised.exception.code, "CODEX_MODEL_UNSUPPORTED_FOR_AUTH")
        self.assertIn("gpt-5.6-sol", str(raised.exception))

    def test_non_codex_provider_does_not_get_codex_model_override(self) -> None:
        app = Phase2Application.__new__(Phase2Application)
        app.cao_base_url = "http://127.0.0.1:9889"
        with patch("ai_meeting_room.product.app.CaoAgentAdapter.create", return_value=object()) as create:
            app._create_cao_adapter(provider="minimax_code", session_id="test", working_directory="/tmp")
        self.assertNotIn("model", create.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
