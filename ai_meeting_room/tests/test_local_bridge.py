from __future__ import annotations

import unittest

from ai_meeting_room.brain.local_bridge import LocalBrainBridge, LocalBrainBridgeError


class LocalBrainBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bridge = LocalBrainBridge(pairing_ttl_seconds=60)
        ticket = self.bridge.create_pairing("meeting-1")
        self.code = ticket["pairingCode"]
        self.token = self.bridge.pair(self.code, "extension-1")["bridgeToken"]

    def test_pairing_is_one_time_and_binding_is_explicit(self) -> None:
        with self.assertRaises(LocalBrainBridgeError):
            self.bridge.pair(self.code, "extension-2")
        with self.assertRaises(LocalBrainBridgeError):
            self.bridge.queue_request(self.token, {"brainRequestId": "r", "meetingId": "meeting-1", "taskId": "t", "prompt": "p"})
        self.bridge.bind(self.token, meeting_id="meeting-1", tab_id=7, conversation_id="conv-1")
        self.bridge.heartbeat(self.token, {"authState": "AUTHENTICATED", "domRecognized": True, "tabId": 7, "conversationBindingId": "conv-1"})
        self.assertTrue(self.bridge.status(self.token)["bound"])

    def test_request_and_response_keep_request_identity(self) -> None:
        self.bridge.bind(self.token, meeting_id="meeting-1", tab_id=7, conversation_id="conv-1")
        self.bridge.queue_request(self.token, {"brainRequestId": "r-1", "meetingId": "meeting-1", "taskId": "t-1", "prompt": "return json"})
        request = self.bridge.poll_request(self.token)
        self.assertEqual(request["type"], "BRAIN_REQUEST")
        self.bridge.submit_response(self.token, brain_request_id="r-1", raw_response='{"decision":"ACCEPT"}')
        self.assertEqual(self.bridge.poll_result(self.token)["brainRequestId"], "r-1")
        with self.assertRaises(LocalBrainBridgeError):
            self.bridge.submit_response(self.token, brain_request_id="wrong", raw_response="x")

    def test_server_must_bind_loopback(self) -> None:
        from ai_meeting_room.brain.local_bridge import LocalBrainBridgeServer
        with self.assertRaises(ValueError):
            LocalBrainBridgeServer(host="0.0.0.0")


if __name__ == "__main__":
    unittest.main()
