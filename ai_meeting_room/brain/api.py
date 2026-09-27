"""Formal API Brain adapter.

This adapter stops at a validated, provider-neutral BrainDecision.  Meeting
Core, TaskEngine, SafetyEngine, and persistence remain outside the provider
boundary and are called only by the application edge.
"""

from __future__ import annotations

from typing import Any, Mapping

from .adapter import BrainAdapter
from .chatgpt_web import BrainBridgeState, BrainRequest
from .protocol import BrainDecisionInvalid, BrainRequestIdempotencyGuard, ValidatedBrainDecision, parse_brain_decision
from .provider import BrainProvider, BrainProviderError, BrainProviderHealth


API_BRAIN_POC_TASK_ID = "api-brain-poc"


class ApiBrainBridge(BrainAdapter):
    """Provider adapter that uses the existing strict Brain protocol."""

    def __init__(self, provider: BrainProvider) -> None:
        self.provider = provider
        self.state = BrainBridgeState.DISCONNECTED
        self.last_request: BrainRequest | None = None
        self.last_decision: ValidatedBrainDecision | None = None
        self.last_error: str | None = None
        self._guard = BrainRequestIdempotencyGuard()
        self._pending_event: Mapping[str, Any] | None = None

    def start(self) -> None:
        self.state = BrainBridgeState.STARTING
        health = self.provider.health_check()
        if not health.healthy:
            self.last_error = health.status
            self.state = BrainBridgeState.AUTH_REQUIRED if health.status == "AUTH_REQUIRED" else BrainBridgeState.ERROR
            return
        self.state = BrainBridgeState.READY

    def stop(self) -> None:
        self.state = BrainBridgeState.DISCONNECTED

    def health_check(self) -> bool:
        if self.state != BrainBridgeState.READY:
            return False
        health = self.provider.health_check()
        if not health.healthy:
            self.last_error = health.status
            self.state = BrainBridgeState.AUTH_REQUIRED if health.status == "AUTH_REQUIRED" else BrainBridgeState.ERROR
            return False
        return True

    def provider_health(self) -> BrainProviderHealth:
        return self.provider.health_check()

    def receive_event(self, event: Mapping[str, Any] | Any) -> None:
        self._pending_event = dict(event) if isinstance(event, Mapping) else {
            "eventId": event.event_id,
            "eventType": event.event_type,
            "meetingId": event.meeting_id,
            "timestamp": event.timestamp,
            "source": event.source,
            "payload": dict(event.payload),
        }

    def decide(self) -> Mapping[str, Any] | None:
        return self.last_decision.as_core_body() if self.last_decision else None

    def dispatch_instruction(self, instruction: Mapping[str, Any]) -> str:
        request = instruction.get("request")
        if not isinstance(request, BrainRequest):
            raise ValueError("ApiBrainBridge requires BrainRequest")
        if self.state != BrainBridgeState.READY or not self.health_check():
            raise BrainProviderError(self.last_error or "BRAIN_UNAVAILABLE", "Brain Provider is not ready")
        if not self._guard.claim(request.brain_request_id):
            raise RuntimeError("duplicate brainRequestId")
        self.last_request = request
        self.state = BrainBridgeState.THINKING
        try:
            self.state = BrainBridgeState.WAITING_RESPONSE
            raw = self.provider.complete(request.prompt)
            self.state = BrainBridgeState.PARSING
            self.last_decision = parse_brain_decision(
                raw,
                brain_request_id=request.brain_request_id,
                expected_task_id=request.task_id,
                allowed_decisions=request.allowed_actions,
            )
            self.state = BrainBridgeState.READY
            return request.brain_request_id
        except Exception as exc:
            self.last_error = f"BRAIN_DECISION_INVALID: {exc}" if isinstance(exc, BrainDecisionInvalid) else type(exc).__name__
            self.state = BrainBridgeState.ERROR
            raise


def validate_api_brain_poc(raw_response: str, *, brain_request_id: str) -> dict[str, Any]:
    """Validate the fixed Phase 3P transport POC response."""
    decision = parse_brain_decision(
        raw_response,
        brain_request_id=brain_request_id,
        expected_task_id=API_BRAIN_POC_TASK_ID,
        allowed_decisions=("ACCEPT",),
    )
    return {
        "decision": {
            "decision": decision.decision,
            "taskId": decision.task_id,
            "reason": decision.reason,
            "instruction": decision.instruction,
            "confidence": decision.confidence,
        },
        "brainRequestId": decision.brain_request_id,
        "validated": True,
    }
