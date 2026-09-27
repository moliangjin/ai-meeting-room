"""Server-side Brain request dispatch through an existing paired extension."""

from __future__ import annotations

from typing import Any, Mapping

from .chatgpt_web import BrainRequest
from .local_bridge import LocalBrainBridge, LocalBrainBridgeError


class BrainDispatchError(RuntimeError):
    """A fail-closed dispatch rejection with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BrainRequestDispatcher:
    """Bridge Product Shell requests to the extension-owned HTTP queue.

    The Product Shell has no token and never calls the extension endpoint over
    HTTP.  ``LocalBrainBridge`` performs the in-process handoff to the exact
    session whose token-authenticated extension heartbeat established pairing,
    tab binding, and DOM health.
    """

    def __init__(self, bridge: LocalBrainBridge) -> None:
        self.bridge = bridge

    @staticmethod
    def _translate(exc: LocalBrainBridgeError) -> BrainDispatchError:
        message = str(exc)
        if message == "extension is not paired":
            return BrainDispatchError("EXTENSION_NOT_PAIRED", message)
        if message == "bridge heartbeat timeout":
            return BrainDispatchError("EXTENSION_HEARTBEAT_LOST", message)
        if message == "extension is not bound":
            return BrainDispatchError("EXTENSION_NOT_BOUND", message)
        if message == "Brain conversation is not bound":
            return BrainDispatchError("CONVERSATION_BINDING_MISSING", message)
        if message == "extension Brain is not ready":
            return BrainDispatchError("BRAIN_UNAVAILABLE", message)
        if message == "duplicate BrainRequest":
            return BrainDispatchError("DUPLICATE_BRAIN_REQUEST", message)
        if message == "another BrainRequest is active":
            return BrainDispatchError("BRAIN_BUSY", message)
        if message == "invalid BrainRequest":
            return BrainDispatchError("INVALID_BRAIN_REQUEST", message)
        return BrainDispatchError("BRAIN_DISPATCH_FAILED", message)

    def dispatch(self, request: BrainRequest | Mapping[str, Any]) -> str:
        """Queue exactly one validated request and return its request id."""
        if not isinstance(request, BrainRequest):
            raise BrainDispatchError("INVALID_BRAIN_REQUEST", "dispatcher requires BrainRequest")
        if not request.brain_request_id or not request.meeting_id or not request.task_id or not request.prompt:
            raise BrainDispatchError("INVALID_BRAIN_REQUEST", "BrainRequest fields are incomplete")
        try:
            self.bridge.queue_request_for_meeting(request.meeting_id, {
                "brainRequestId": request.brain_request_id,
                "meetingId": request.meeting_id,
                "taskId": request.task_id,
                "prompt": request.prompt,
            })
        except LocalBrainBridgeError as exc:
            raise self._translate(exc) from exc
        return request.brain_request_id
