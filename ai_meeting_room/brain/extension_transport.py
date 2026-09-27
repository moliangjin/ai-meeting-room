"""localhost transport for the user-installed Manifest V3 Brain extension."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Mapping
from uuid import uuid4

from .chatgpt_web import BrainTransport, BrainRequest
from .dispatcher import BrainRequestDispatcher
from .local_bridge import LocalBrainBridge, LocalBrainBridgeError


class BrowserExtensionTransport(BrainTransport):
    """HTTP long-poll transport; the extension owns all ChatGPT DOM access."""

    def __init__(self, base_url: str = "http://127.0.0.1:9890", *, bridge_token: str | None = None, timeout_seconds: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.bridge_token = bridge_token
        self.timeout_seconds = timeout_seconds
        self.connection_id = str(uuid4())
        self._request: BrainRequest | None = None
        self._response: str | None = None
        self._response_request_id: str | None = None
        self._conversation_id: str | None = None

    def _request_json(self, method: str, path: str, body: Mapping[str, Any] | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        data = json.dumps(dict(body or {}), ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json", "Content-Type": "application/json", "Origin": "chrome-extension://aimr-brain"}
        if self.bridge_token:
            headers["X-AIMR-Bridge-Token"] = self.bridge_token
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError("Brain extension transport unavailable") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("invalid Brain extension transport response")
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        return payload

    def pair(self, pairing_code: str) -> None:
        result = self._request_json("POST", "/bridge/hello", {"pairingCode": pairing_code, "connectionId": self.connection_id})
        self.bridge_token = str(result["bridgeToken"])

    def bind(self, meeting_id: str, tab_id: int, conversation_id: str) -> None:
        if not self.bridge_token:
            raise RuntimeError("extension transport is not paired")
        self._request_json("POST", "/bridge/bind", {"meetingId": meeting_id, "tabId": tab_id, "conversationBindingId": conversation_id})
        self._conversation_id = conversation_id

    def connect(self) -> None:
        if not self.bridge_token:
            return
        self._request_json("GET", "/bridge/status")

    def auth_status(self) -> str:
        if not self.bridge_token:
            return "AUTH_REQUIRED"
        try:
            result = self._request_json("GET", "/bridge/status")
        except RuntimeError:
            return "UNKNOWN"
        if result.get("authState") == "AUTHENTICATED" and result.get("bound") and result.get("domRecognized"):
            return "AUTHENTICATED"
        if result.get("authState") == "AUTH_REQUIRED":
            return "AUTH_REQUIRED"
        return "UNKNOWN"

    def open_conversation(self, conversation_id: str | None = None) -> str:
        expected = conversation_id or self._conversation_id
        result = self._request_json("GET", "/bridge/status")
        actual = result.get("conversationBindingId")
        if not expected or actual != expected:
            raise RuntimeError("conversation binding mismatch")
        return str(actual)

    def prepare_request(self, request: BrainRequest) -> None:
        self._request = request
        self._response = None
        self._response_request_id = None

    def send_prompt(self, prompt: str) -> None:
        if not self.bridge_token or self._request is None:
            raise RuntimeError("extension transport request is not prepared")
        self._request_json("POST", "/bridge/request", {
            "brainRequestId": self._request.brain_request_id,
            "meetingId": self._request.meeting_id,
            "taskId": self._request.task_id,
            "prompt": prompt,
        })

    def wait_for_completion(self, timeout_seconds: float) -> None:
        if not self.bridge_token or not self._request:
            raise RuntimeError("extension transport request is not prepared")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            result = self._request_json("GET", "/bridge/result", timeout=min(self.timeout_seconds, max(1.0, deadline - time.monotonic())))
            if result.get("type") == "BRAIN_RESPONSE" and result.get("brainRequestId") == self._request.brain_request_id:
                self._response = str(result.get("rawResponse", ""))
                self._response_request_id = str(result["brainRequestId"])
                return
            if result.get("type") == "ERROR":
                raise RuntimeError(str(result.get("reason") or "extension reported error"))
            status = self._request_json("GET", "/bridge/status")
            if status.get("error"):
                raise RuntimeError(str(status["error"]))
        raise TimeoutError("Brain extension response timeout")

    def read_response(self) -> str:
        if self._response is None or self._response_request_id != (self._request.brain_request_id if self._request else None):
            raise RuntimeError("Brain extension response is not available")
        return self._response

    def close(self) -> None:
        # The extension may remain paired for another Meeting; explicit
        # unbind/disconnect is a Product Shell operation, not implicit cleanup.
        self._request = None


class BoundExtensionTransport(BrainTransport):
    """Product-Shell-side transport for an already paired extension session.

    It intentionally has no bridge token. Requests enter the same Local Brain
    Bridge queue that the extension polls, and responses still have to arrive
    through the extension's token-authenticated response endpoint.
    """

    def __init__(self, bridge: LocalBrainBridge, meeting_id: str, *, timeout_seconds: float = 180.0) -> None:
        self.bridge = bridge
        self.meeting_id = meeting_id
        self.timeout_seconds = timeout_seconds
        self.dispatcher = BrainRequestDispatcher(bridge)
        self._request: BrainRequest | None = None
        self._response: str | None = None
        self._response_request_id: str | None = None

    def _status(self) -> dict[str, Any]:
        try:
            return self.bridge.status_for_meeting(self.meeting_id)
        except LocalBrainBridgeError as exc:
            raise RuntimeError(str(exc)) from exc

    def connect(self) -> None:
        self._status()

    def auth_status(self) -> str:
        try:
            status = self._status()
        except RuntimeError:
            return "UNKNOWN"
        if status.get("authState") == "AUTHENTICATED" and status.get("bound") and status.get("domRecognized"):
            return "AUTHENTICATED"
        if status.get("authState") == "AUTH_REQUIRED":
            return "AUTH_REQUIRED"
        return "UNKNOWN"

    def open_conversation(self, conversation_id: str | None = None) -> str:
        status = self._status()
        actual = status.get("conversationBindingId")
        if not actual or (conversation_id and actual != conversation_id):
            raise RuntimeError("conversation binding mismatch")
        return str(actual)

    def prepare_request(self, request: BrainRequest) -> None:
        self._request = request
        self._response = None
        self._response_request_id = None

    def send_prompt(self, prompt: str) -> None:
        if self._request is None:
            raise RuntimeError("extension transport request is not prepared")
        if prompt != self._request.prompt:
            raise RuntimeError("BrainRequest prompt mismatch")
        self.dispatcher.dispatch(self._request)

    def wait_for_completion(self, timeout_seconds: float) -> None:
        if self._request is None:
            raise RuntimeError("extension transport request is not prepared")
        deadline = time.monotonic() + min(timeout_seconds, self.timeout_seconds)
        while time.monotonic() < deadline:
            try:
                result = self.bridge.poll_result_for_meeting(self.meeting_id, self._request.brain_request_id)
            except LocalBrainBridgeError as exc:
                raise RuntimeError(str(exc)) from exc
            if result.get("type") == "BRAIN_RESPONSE":
                self._response = str(result.get("rawResponse", ""))
                self._response_request_id = str(result.get("brainRequestId"))
                return
            if result.get("type") == "ERROR":
                raise RuntimeError(str(result.get("reason") or "extension reported error"))
            time.sleep(0.25)
        raise TimeoutError("Brain extension response timeout")

    def read_response(self) -> str:
        if self._response is None or self._response_request_id != (self._request.brain_request_id if self._request else None):
            raise RuntimeError("Brain extension response is not available")
        return self._response

    def close(self) -> None:
        self._request = None
