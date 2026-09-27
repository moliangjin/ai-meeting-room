from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_meeting_room.brain.config import BrainProviderConfigError, BrainProviderConfigValidator
from ai_meeting_room.brain.provider import BrainProviderError
from ai_meeting_room.brain.secrets import BrainSecretStore
from ai_meeting_room.persistence.sqlite_store import SQLiteStore
from ai_meeting_room.product.app import Phase2Application


class FakeSecretStore(BrainSecretStore):
    def __init__(self):
        self.value = None
        self.saved = []

    def save(self, secret: str) -> str:
        self.value = secret
        self.saved.append(secret)
        return "brain-provider"

    def exists(self, reference: str) -> bool:
        return bool(self.value)

    def resolve(self, reference: str) -> str:
        if not self.value:
            raise RuntimeError("missing")
        return self.value

    def delete(self, reference: str) -> bool:
        existed = bool(self.value)
        self.value = None
        return existed


class BrainProviderConfigTests(unittest.TestCase):
    def make_app(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app = Phase2Application(SQLiteStore(Path(self.tempdir.name) / "meeting.db"))
        app.brain_secret_store = FakeSecretStore()
        return app

    def tearDown(self):
        if hasattr(self, "tempdir"):
            self.tempdir.cleanup()

    def valid_payload(self, **overrides):
        value = {"provider": "openai-compatible", "baseUrl": "https://brain.example/v1/chat/completions", "model": "brain-model", "timeout": 30, "enabled": True}
        value.update(overrides)
        return value

    def test_secret_never_returned_by_config_api(self):
        app = self.make_app()
        app.save_brain_provider_config(self.valid_payload(apiCredential="top-secret"))
        response = app.brain_provider_status()
        self.assertTrue(response["config"]["credentialConfigured"])
        self.assertNotIn("top-secret", repr(response))
        self.assertNotIn("apiCredential", repr(response))

    def test_secret_saved_to_secret_store(self):
        app = self.make_app()
        app.save_brain_provider_config(self.valid_payload(apiCredential="top-secret"))
        self.assertEqual(app.brain_secret_store.saved, ["top-secret"])
        self.assertIsNone(app.store.get_brain_provider_config().get("apiCredential"))

    def test_config_reports_credential_boolean_only(self):
        app = self.make_app()
        response = app.brain_provider_status()
        self.assertFalse(response["config"]["credentialConfigured"])
        self.assertNotIn("credential", response["config"])
        app.save_brain_provider_config(self.valid_payload(apiCredential="top-secret"))
        self.assertTrue(app.brain_provider_status()["config"]["credentialConfigured"])

    def test_missing_secret_returns_config_required(self):
        with self.assertRaises(BrainProviderConfigError) as caught:
            BrainProviderConfigValidator.validate(self.valid_payload(), credential_configured=False)
        self.assertEqual(caught.exception.code, "CONFIG_REQUIRED")

    def test_invalid_base_url_rejected(self):
        with self.assertRaises(BrainProviderConfigError):
            BrainProviderConfigValidator.validate(self.valid_payload(baseUrl="file:///secret"), credential_configured=True)

    def test_test_connection_requires_valid_config(self):
        app = self.make_app()
        with patch("ai_meeting_room.product.app.OpenAICompatibleProvider.complete") as complete:
            result = app.test_brain_provider()
        self.assertEqual(result["status"], "CONFIG_REQUIRED")
        complete.assert_not_called()

    def test_remove_credential_invalidates_provider(self):
        app = self.make_app()
        app.save_brain_provider_config(self.valid_payload(apiCredential="top-secret"))
        app.remove_brain_provider_credential()
        result = app.brain_provider_status()
        self.assertFalse(result["config"]["credentialConfigured"])
        self.assertEqual(result["health"]["status"], "CONFIG_REQUIRED")

    def test_provider_error_classification(self):
        app = self.make_app()
        app.save_brain_provider_config(self.valid_payload(apiCredential="top-secret"))
        with patch("ai_meeting_room.product.app.OpenAICompatibleProvider.complete", side_effect=BrainProviderError("RATE_LIMITED")):
            result = app.test_brain_provider()
        self.assertEqual(result["status"], "RATE_LIMITED")

    def test_manual_brain_still_available_without_api_config(self):
        app = self.make_app()
        workspace = Path(self.tempdir.name) / "workspace"
        workspace.mkdir()
        meeting_id = app.create_meeting("manual", str(workspace))["meeting"]["meeting_id"]
        self.assertEqual(app.snapshot(meeting_id)["brain"]["name"], "ManualBrainBridge")


if __name__ == "__main__":
    unittest.main()
