from __future__ import annotations

import json
import threading
import time
import unittest

from ai_meeting_room.brain.chatgpt_web import BrainRequest, BrainBridgeState, ChatGPTWebBrainBridge
from ai_meeting_room.brain.dispatcher import BrainDispatchError, BrainRequestDispatcher
from ai_meeting_room.brain.extension_transport import BoundExtensionTransport
from ai_meeting_room.brain.local_bridge import LocalBrainBridge


class BrainRequestDispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bridge = LocalBrainBridge(pairing_ttl_seconds=60)
        ticket = self.bridge.create_pairing("meeting-1")
        self.token = self.bridge.pair(ticket["pairingCode"], "extension-1")["bridgeToken"]
        self.bridge.bind(self.token, meeting_id="meeting-1", tab_id=7, conversation_id="conv-1")
        self.bridge.heartbeat(self.token, {
            "authState": "AUTHENTICATED",
            "domRecognized": True,
            "tabId": 7,
            "conversationBindingId": "conv-1",
        })
        self.request = BrainRequest("r-1", "meeting-1", "task-1", "return JSON", ("ACCEPT",))

    def test_dispatch_uses_existing_bound_session_and_keeps_secret_private(self) -> None:
        dispatcher = BrainRequestDispatcher(self.bridge)
        self.assertEqual(dispatcher.dispatch(self.request), "r-1")
        queued = self.bridge.poll_request(self.token)
        self.assertEqual(queued["brainRequestId"], "r-1")
        self.bridge.submit_response(self.token, brain_request_id="r-1", raw_response='{"ok":true}')
        result = self.bridge.poll_result_for_meeting("meeting-1", "r-1")
        self.assertEqual(result["type"], "BRAIN_RESPONSE")
        self.assertNotIn("bridgeToken", self.bridge.status_for_meeting("meeting-1"))
        self.assertNotIn(self.token, json.dumps(result))

    def test_duplicate_and_busy_requests_fail_closed(self) -> None:
        dispatcher = BrainRequestDispatcher(self.bridge)
        dispatcher.dispatch(self.request)
        with self.assertRaisesRegex(BrainDispatchError, "duplicate BrainRequest"):
            dispatcher.dispatch(self.request)
        other = BrainRequest("r-2", "meeting-1", "task-2", "return JSON", ("ACCEPT",))
        with self.assertRaisesRegex(BrainDispatchError, "another BrainRequest is active"):
            dispatcher.dispatch(other)

    def test_pairing_binding_and_heartbeat_are_required(self) -> None:
        unpaired = LocalBrainBridge()
        with self.assertRaisesRegex(BrainDispatchError, "extension is not paired"):
            BrainRequestDispatcher(unpaired).dispatch(self.request)

        unbound = LocalBrainBridge()
        ticket = unbound.create_pairing("meeting-1")
        unbound.pair(ticket["pairingCode"], "extension-1")
        with self.assertRaisesRegex(BrainDispatchError, "extension is not bound"):
            BrainRequestDispatcher(unbound).dispatch(self.request)

        expired = LocalBrainBridge(pairing_ttl_seconds=60, heartbeat_timeout_seconds=0.001)
        ticket = expired.create_pairing("meeting-1")
        token = expired.pair(ticket["pairingCode"], "extension-1")["bridgeToken"]
        expired.bind(token, meeting_id="meeting-1", tab_id=7, conversation_id="conv-1")
        expired.heartbeat(token, {"authState": "AUTHENTICATED", "domRecognized": True, "tabId": 7, "conversationBindingId": "conv-1"})
        time.sleep(0.01)
        with self.assertRaisesRegex(BrainDispatchError, "bridge heartbeat timeout"):
            BrainRequestDispatcher(expired).dispatch(self.request)

    def test_bound_transport_delivers_extension_response_to_strict_parser(self) -> None:
        transport = BoundExtensionTransport(self.bridge, "meeting-1", timeout_seconds=2)
        brain = ChatGPTWebBrainBridge(transport, response_timeout_seconds=2)
        brain.start()
        self.assertEqual(brain.state, BrainBridgeState.READY)

        def extension_side() -> None:
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                event = self.bridge.poll_request(self.token)
                if event["type"] == "BRAIN_REQUEST":
                    raw = json.dumps({
                        "decision": "ACCEPT",
                        "taskId": "task-1",
                        "reason": "extension transport poc",
                        "instruction": "",
                        "confidence": 1.0,
                    })
                    self.bridge.submit_response(self.token, brain_request_id="r-1", raw_response=raw)
                    return
                time.sleep(0.01)

        worker = threading.Thread(target=extension_side)
        worker.start()
        brain.dispatch_instruction({"request": self.request})
        worker.join(timeout=1)
        self.assertEqual(brain.decide()["type"], "ACCEPT")
        self.assertEqual(brain.state, BrainBridgeState.READY)


if __name__ == "__main__":
    unittest.main()
