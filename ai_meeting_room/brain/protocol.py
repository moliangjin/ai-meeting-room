"""Strict, provider-neutral BrainDecision transport validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable


ALLOWED_DECISIONS = frozenset({"ACCEPT", "REWORK", "REJECT", "PAUSE", "RESUME", "COMPLETE_MEETING"})


class BrainDecisionInvalid(ValueError):
    """Raised when a Web Brain response cannot be safely admitted."""


@dataclass(frozen=True)
class ValidatedBrainDecision:
    """Validated transport value; MeetingCore remains the business authority."""

    decision: str
    task_id: str
    reason: str
    instruction: str
    confidence: float
    brain_request_id: str

    def as_core_body(self) -> dict[str, str | float]:
        return {
            "type": self.decision,
            "relatedTaskId": self.task_id,
            "reason": self.reason,
            "instruction": self.instruction,
            "confidence": self.confidence,
            "brainRequestId": self.brain_request_id,
        }


def _unwrap_json_document(raw: str) -> str:
    value = raw.strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) < 3 or not lines[0].startswith("```") or lines[-1].strip() != "```":
            raise BrainDecisionInvalid("response incomplete")
        value = "\n".join(lines[1:-1]).strip()
    if not value:
        raise BrainDecisionInvalid("response incomplete")
    return value


def parse_brain_decision(
    raw: str,
    *,
    brain_request_id: str,
    expected_task_id: str,
    allowed_decisions: Iterable[str] = ALLOWED_DECISIONS,
) -> ValidatedBrainDecision:
    """Parse exactly one JSON object and fail closed on every mismatch."""
    if not isinstance(raw, str):
        raise BrainDecisionInvalid("response must be text")
    try:
        payload = json.loads(_unwrap_json_document(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        raise BrainDecisionInvalid("malformed JSON") from exc
    if not isinstance(payload, dict):
        raise BrainDecisionInvalid("response must be a JSON object")

    required = {"decision", "taskId", "reason", "instruction", "confidence"}
    if set(payload) != required:
        missing = required - set(payload)
        extra = set(payload) - required
        if missing:
            raise BrainDecisionInvalid(f"missing fields: {','.join(sorted(missing))}")
        raise BrainDecisionInvalid(f"unknown fields: {','.join(sorted(extra))}")

    decision = payload["decision"]
    task_id = payload["taskId"]
    reason = payload["reason"]
    instruction = payload["instruction"]
    confidence = payload["confidence"]
    if not isinstance(decision, str) or decision not in set(allowed_decisions):
        raise BrainDecisionInvalid("unknown decision")
    if not isinstance(task_id, str) or not task_id:
        raise BrainDecisionInvalid("missing taskId")
    if task_id != expected_task_id:
        raise BrainDecisionInvalid("taskId mismatch")
    if not isinstance(reason, str) or not reason.strip():
        raise BrainDecisionInvalid("reason must be non-empty")
    if not isinstance(instruction, str):
        raise BrainDecisionInvalid("instruction must be text")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise BrainDecisionInvalid("confidence must be between 0 and 1")
    if not isinstance(brain_request_id, str) or not brain_request_id:
        raise BrainDecisionInvalid("missing brainRequestId")
    return ValidatedBrainDecision(decision, task_id, reason.strip(), instruction, float(confidence), brain_request_id)


class BrainRequestIdempotencyGuard:
    """In-memory request guard; durable request metadata belongs to the app store."""

    def __init__(self) -> None:
        self._completed: set[str] = set()

    def claim(self, brain_request_id: str) -> bool:
        if brain_request_id in self._completed:
            return False
        self._completed.add(brain_request_id)
        return True

    def seen(self, brain_request_id: str) -> bool:
        return brain_request_id in self._completed
