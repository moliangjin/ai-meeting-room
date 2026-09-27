"""Manual Brain inbox and provider-neutral decision records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Mapping
from uuid import uuid4

from .adapter import BrainAdapter
from ..models import utc_now


@dataclass(frozen=True)
class BrainEvent:
    meeting_id: str
    event_id: str
    agent_id: str | None
    task_id: str | None
    event_type: str
    summary: str
    artifact_references: tuple[str, ...] = ()
    timestamp: str = field(default_factory=utc_now)
    resolved: bool = False

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["meetingId"] = result.pop("meeting_id")
        result["eventId"] = result.pop("event_id")
        result["agentId"] = result.pop("agent_id")
        result["taskId"] = result.pop("task_id")
        result["eventType"] = result.pop("event_type")
        result["artifactReferences"] = list(result.pop("artifact_references"))
        return result


@dataclass(frozen=True)
class BrainDecision:
    decision_id: str
    meeting_id: str
    type: str
    target_agent_id: str | None
    instruction: str | None
    related_task_id: str | None
    reason: str
    timestamp: str = field(default_factory=utc_now)
    # Transport metadata is persisted with the decision so a bridge retry can
    # be audited without making the Core understand a browser protocol.
    brain_request_id: str | None = None
    confidence: float | None = None
    packet_id: str | None = None
    result_id: str | None = None
    brain_participant_id: str | None = None
    transport: str | None = None
    human_transferred: bool = False

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["decisionId"] = result.pop("decision_id")
        result["meetingId"] = result.pop("meeting_id")
        result["targetAgentId"] = result.pop("target_agent_id")
        result["relatedTaskId"] = result.pop("related_task_id")
        result["brainRequestId"] = result.pop("brain_request_id")
        result["packetId"] = result.pop("packet_id")
        result["resultId"] = result.pop("result_id")
        result["brainParticipantId"] = result.pop("brain_participant_id")
        result["humanTransferred"] = result.pop("human_transferred")
        return result


class BrainInbox:
    def __init__(self, meeting_id: str, persist_event=None, persist_decision=None) -> None:
        self.meeting_id = meeting_id
        self._events: list[BrainEvent] = []
        self._decisions: list[BrainDecision] = []
        self._persist_event = persist_event
        self._persist_decision = persist_decision

    def receive(self, event: Mapping[str, Any] | Any) -> BrainEvent:
        if hasattr(event, "event_type"):
            payload = dict(event.payload)
            event_id = event.event_id
            event_type = event.event_type
            meeting_id = event.meeting_id
            source = event.source
        else:
            payload = dict(event.get("payload", {}))
            event_id = str(event.get("eventId") or uuid4())
            event_type = str(event.get("eventType") or "UNKNOWN")
            meeting_id = str(event.get("meetingId") or self.meeting_id)
            source = str(event.get("source") or "unknown")
        agent_id = payload.get("agentId") or payload.get("agent_id")
        task_id = payload.get("taskId") or payload.get("task_id")
        summary = self._summary(event_type, payload, source)
        item = BrainEvent(meeting_id, event_id, agent_id, task_id, event_type, summary)
        self._events.append(item)
        if self._persist_event:
            self._persist_event(self.meeting_id, item.as_dict())
        return item

    @staticmethod
    def _summary(event_type: str, payload: Mapping[str, Any], source: str) -> str:
        status = payload.get("status") or payload.get("to") or ""
        suffix = f" ({status})" if status else ""
        return f"{event_type}{suffix} from {source}"

    def submit(self, *, decision_type: str, target_agent_id: str | None = None, instruction: str | None = None, related_task_id: str | None = None, reason: str = "manual operator decision", brain_request_id: str | None = None, confidence: float | None = None, decision_id: str | None = None, packet_id: str | None = None, result_id: str | None = None, brain_participant_id: str | None = None, transport: str | None = None, human_transferred: bool = False) -> BrainDecision:
        allowed = {"DISPATCH", "REWORK", "ACCEPT", "REJECT", "REVIEW", "PAUSE", "RESUME", "COMPLETE_MEETING"}
        normalized = decision_type.upper()
        if normalized not in allowed:
            raise ValueError(f"unsupported BrainDecision type: {decision_type}")
        identity = decision_id or str(uuid4())
        if any(item.decision_id == identity for item in self._decisions):
            raise ValueError("duplicate BrainDecision id")
        decision = BrainDecision(
            identity, self.meeting_id, normalized, target_agent_id, instruction,
            related_task_id, reason, brain_request_id=brain_request_id,
            confidence=confidence, packet_id=packet_id, result_id=result_id,
            brain_participant_id=brain_participant_id, transport=transport,
            human_transferred=human_transferred,
        )
        self._decisions.append(decision)
        if self._persist_decision:
            self._persist_decision(self.meeting_id, decision.as_dict())
        return decision

    def resolve_task(self, task_id: str) -> int:
        """Resolve BrainInbox items associated with an accepted task."""
        resolved = 0
        updated: list[BrainEvent] = []
        for item in self._events:
            if item.task_id == task_id and not item.resolved:
                item = replace(item, resolved=True)
                resolved += 1
                if self._persist_event:
                    self._persist_event(self.meeting_id, item.as_dict())
            updated.append(item)
        self._events = updated
        return resolved

    def events(self) -> tuple[BrainEvent, ...]:
        return tuple(self._events)

    def decisions(self) -> tuple[BrainDecision, ...]:
        return tuple(self._decisions)


class ManualBrainBridge(BrainAdapter):
    """Human-operated Brain implementation; it is intentionally not an AI."""

    def __init__(self, inbox: BrainInbox) -> None:
        self.inbox = inbox
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def health_check(self) -> bool:
        return self.started

    def receive_event(self, event: Mapping[str, Any]) -> None:
        self.inbox.receive(event)

    def decide(self) -> Mapping[str, Any] | None:
        return self.inbox.decisions()[-1].as_dict() if self.inbox.decisions() else None

    def dispatch_instruction(self, instruction: Mapping[str, Any]) -> str:
        raise RuntimeError("ManualBrainBridge produces BrainDecision; MeetingCore performs dispatch")
