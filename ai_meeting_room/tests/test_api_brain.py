from __future__ import annotations

import json
import unittest

from ai_meeting_room.brain.api import API_BRAIN_POC_TASK_ID, ApiBrainBridge, validate_api_brain_poc
from ai_meeting_room.brain.chatgpt_web import BrainBridgeState, BrainRequest
from ai_meeting_room.brain.protocol import BrainDecisionInvalid
from ai_meeting_room.brain.provider import BrainProviderConfig, BrainProviderError, OpenAICompatibleProvider


class _Response:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class ApiBrainTests(unittest.TestCase):
    def config(self, **kwargs):
        values = {
            "provider": "openai-compatible",
            "model": "test-model",
            "endpoint": "https://provider.invalid/v1/chat/completions",
            "authentication_reference": "TEST_BRAIN_SECRET",
            "max_retries": 0,
        }
        values.update(kwargs)
        return BrainProviderConfig(**values)

    def request(self):
        return BrainRequest(
            "req-api-1", "meeting-1", API_BRAIN_POC_TASK_ID,
            "Return the fixed JSON object.", ("ACCEPT",), created_at="now",
        )

    def test_public_config_never_contains_secret(self):
        env = {"TEST_BRAIN_SECRET": "do-not-return"}
        public = self.config().as_public_dict(env)
        self.assertTrue(public["credentialPresent"])
        self.assertNotIn("do-not-return", repr(public))

    def test_missing_credential_is_not_healthy(self):
        health = OpenAICompatibleProvider(self.config(), environ={}).health_check()
        self.assertEqual(health.status, "AUTH_REQUIRED")

    def test_real_provider_response_is_sent_to_existing_parser(self):
        expected = {"decision": "ACCEPT", "taskId": API_BRAIN_POC_TASK_ID, "reason": "api brain poc", "instruction": "", "confidence": 1.0}
        provider = OpenAICompatibleProvider(
            self.config(), environ={"TEST_BRAIN_SECRET": "secret"},
            opener=lambda request, timeout: _Response({"choices": [{"message": {"content": json.dumps(expected)}}]}),
        )
        bridge = ApiBrainBridge(provider)
        bridge.start()
        self.assertTrue(bridge.health_check())
        bridge.dispatch_instruction({"request": self.request()})
        self.assertEqual(bridge.decide()["type"], "ACCEPT")
        self.assertEqual(bridge.state, BrainBridgeState.READY)

    def test_invalid_api_response_fails_closed(self):
        provider = OpenAICompatibleProvider(
            self.config(), environ={"TEST_BRAIN_SECRET": "secret"},
            opener=lambda request, timeout: _Response({"choices": [{"message": {"content": "not json"}}]}),
        )
        bridge = ApiBrainBridge(provider)
        bridge.start()
        with self.assertRaises(BrainDecisionInvalid):
            bridge.dispatch_instruction({"request": self.request()})
        self.assertEqual(bridge.state, BrainBridgeState.ERROR)

    def test_api_failure_does_not_touch_core(self):
        with open("ai_meeting_room/brain/api.py", encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("from ..core", source)
        self.assertNotIn("from ..tasks", source)
        self.assertNotIn("from ..persistence", source)

    def test_poc_validator_is_strict(self):
        raw = json.dumps({"decision": "ACCEPT", "taskId": API_BRAIN_POC_TASK_ID, "reason": "api brain poc", "instruction": "", "confidence": 1.0})
        self.assertTrue(validate_api_brain_poc(raw, brain_request_id="req-api-1")["validated"])
        with self.assertRaises(BrainDecisionInvalid):
            validate_api_brain_poc(raw.replace(API_BRAIN_POC_TASK_ID, "wrong"), brain_request_id="req-api-1")


if __name__ == "__main__":
    unittest.main()
