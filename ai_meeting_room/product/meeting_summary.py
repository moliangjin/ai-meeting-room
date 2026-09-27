"""Safe Markdown summaries assembled from the formal Meeting boundaries.

The formatter deliberately accepts already-owned domain and persistence
records rather than a frontend snapshot.  It only renders fields needed by a
Meeting operator and never serializes raw event payloads, runtime objects, or
database rows wholesale.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Iterable, Mapping

from ..brain.manual_gpt import ManualGPTBrainTransport


_MAX_RESULT_CHARS = 600
_MAX_REASON_CHARS = 500
_MAX_REWORK_CHARS = 1000
_REDACTED = "[REDACTED: sensitive content omitted]"


@dataclass(frozen=True)
class MeetingSummaryDocument:
    """The safe export body plus metadata suitable for an audit event."""

    markdown: str
    redacted_field_count: int

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "format": "markdown",
            "characterCount": len(self.markdown),
            "lineCount": self.markdown.count("\n") + 1,
            "redactedFieldCount": self.redacted_field_count,
            "contentSha256": sha256(self.markdown.encode("utf-8")).hexdigest(),
        }


class MeetingSummaryExporter:
    """Render an operator-facing summary from formal Meeting state.

    `events`, `brain_events`, and `brain_decisions` are persistence records,
    not arbitrary caller input.  Even so, all text is bounded and passed
    through the existing Brain Packet sensitive-content detector before it is
    rendered.  This keeps a malicious Agent result or Brain reason from
    becoming an export exfiltration path.
    """

    _SAFETY_EVENT_TYPES = frozenset({
        "CircuitBreakerOpened",
        "BrainCircuitBreakerOpened",
        "SafetyMonitorFailed",
        "DispatchBlocked",
        "TaskDispatchBlocked",
    })
    _PAUSE_RESUME_EVENT_TYPES = frozenset({
        "MeetingPaused",
        "MeetingRecovering",
        "MeetingResumed",
    })
    # Export requests are audit metadata, and heartbeats are volatile runtime
    # telemetry, not Meeting business state.  Both travel through the same
    # event bus, so the Manual Brain inbox may retain corresponding activity
    # records.  Excluding them keeps a later export idempotent while the
    # durable rows remain available in SQLite for compliance/diagnostics.
    _NON_BUSINESS_EVENT_TYPES = frozenset({
        "MEETING_SUMMARY_EXPORTED",
        "AgentHeartbeatReceived",
    })

    def __init__(self) -> None:
        self._redacted_field_count = 0

    def build(
        self,
        *,
        meeting: Any,
        agents: Iterable[Any],
        tasks: Iterable[Any],
        safety: Mapping[str, Any],
        events: Iterable[Mapping[str, Any]],
        brain_events: Iterable[Mapping[str, Any]],
        brain_decisions: Iterable[Mapping[str, Any]],
        brain_participant: Mapping[str, Any] | None,
        brain_transport: str,
    ) -> MeetingSummaryDocument:
        """Build a bounded Markdown document without mutating Meeting state."""
        self._redacted_field_count = 0
        event_rows = self._sorted_rows(
            row for row in events
            if row.get("event_type") not in self._NON_BUSINESS_EVENT_TYPES
        )
        decision_rows = self._sorted_rows(brain_decisions)
        brain_event_rows = self._sorted_rows(
            row for row in brain_events
            if row.get("eventType") not in self._NON_BUSINESS_EVENT_TYPES
        )
        agent_rows = list(agents)
        # Runtime insertion order and SQLite hydration order are allowed to
        # differ.  The export is a durable artifact, so order tasks by their
        # persisted chronology with the immutable ID as a deterministic tie
        # breaker instead of inheriting either container's incidental order.
        task_rows = sorted(
            tasks,
            key=lambda task: (
                str(getattr(task, "created_at", "") or ""),
                str(getattr(task, "task_id", "") or ""),
            ),
        )

        lines: list[str] = [
            f"# Meeting Summary — {self._inline(getattr(meeting, 'name', 'Untitled Meeting'), 240)}",
            "",
            "## Meeting",
            f"- Name: {self._inline(getattr(meeting, 'name', 'Untitled Meeting'), 240)}",
            f"- ID: `{self._inline(getattr(meeting, 'meeting_id', ''), 240)}`",
            f"- Current / final state: `{self._inline(self._value(getattr(meeting, 'status', 'UNKNOWN')), 80)}`",
            f"- Created: {self._inline(getattr(meeting, 'created_at', ''), 80)}",
            f"- Started: {self._inline(getattr(meeting, 'started_at', None) or '—', 80)}",
            f"- Completed: {self._inline(getattr(meeting, 'completed_at', None) or '—', 80)}",
            f"- Completion state: {self._completion_state(getattr(meeting, 'status', 'UNKNOWN'))}",
            "",
            "## Participants",
        ]

        participant = brain_participant or {}
        if participant:
            lines.append(
                "- Brain: "
                f"{self._inline(participant.get('participantId') or 'unknown', 180)} "
                f"(transport: {self._inline(participant.get('provider') or brain_transport, 120)}, "
                f"health: {self._inline(participant.get('health') or 'UNKNOWN', 80)})"
            )
        else:
            lines.append(f"- Brain: not joined (transport: {self._inline(brain_transport, 120)})")
        if agent_rows:
            for agent in agent_rows:
                lines.append(
                    "- Agent: "
                    f"{self._inline(getattr(agent, 'display_name', ''), 180)} "
                    f"[{self._inline(getattr(agent, 'provider', ''), 120)}] "
                    f"status={self._inline(self._value(getattr(agent, 'status', 'UNKNOWN')), 80)}, "
                    f"health={self._inline(self._value(getattr(agent, 'health', 'UNKNOWN')), 80)}"
                )
        else:
            lines.append("- Agent: none")

        lines.extend([
            "",
            "## Brain Transport",
            f"- Transport: `{self._inline(brain_transport, 120)}`",
            f"- Brain activity records: {len(brain_event_rows)}",
            "",
            "## Tasks",
        ])
        if task_rows:
            agent_names = {getattr(agent, "agent_id", ""): getattr(agent, "display_name", "") for agent in agent_rows}
            for task in task_rows:
                task_id = self._inline(getattr(task, "task_id", ""), 180)
                title = self._inline(getattr(task, "title", "Untitled task"), 240)
                status = self._inline(self._value(getattr(task, "status", "UNKNOWN")), 80)
                assigned_id = getattr(task, "assigned_agent_id", None)
                assigned = agent_names.get(assigned_id, assigned_id or "unassigned")
                result = self._inline(getattr(task, "result", None) or "No Agent Result", _MAX_RESULT_CHARS)
                accepted = "yes" if bool(getattr(task, "accepted", False)) else "no"
                lines.extend([
                    f"### {title}",
                    f"- Task ID: `{task_id}`",
                    f"- Status: `{status}`",
                    f"- Agent: {self._inline(assigned, 180)}",
                    f"- Accepted: {accepted}",
                    f"- Agent Result summary: {result}",
                    "",
                ])
        else:
            lines.append("- No tasks")

        lines.extend(["", "## GPT Brain Decisions"])
        if decision_rows:
            for decision in decision_rows:
                kind = self._inline(decision.get("type") or decision.get("decision") or "UNKNOWN", 80)
                task_id = self._inline(decision.get("relatedTaskId") or decision.get("taskId") or "—", 180)
                timestamp = self._inline(decision.get("timestamp") or decision.get("createdAt") or "", 80)
                reason = self._inline(decision.get("reason") or "—", _MAX_REASON_CHARS)
                lines.extend([
                    f"- `{kind}` · task `{task_id}` · {timestamp}",
                    f"  - Reason: {reason}",
                ])
                if str(kind).upper() == "REWORK":
                    instruction = decision.get("instruction") or decision.get("reworkInstruction") or "—"
                    lines.append(f"  - REWORK instruction: {self._inline(instruction, _MAX_REWORK_CHARS)}")
        else:
            lines.append("- No persisted GPT Brain decisions")

        lines.extend(["", "## ACCEPT Records"])
        accept_records = self._accept_records(event_rows, decision_rows, task_rows)
        if accept_records:
            for record in accept_records:
                lines.append(
                    f"- Task `{self._inline(record['taskId'], 180)}` · "
                    f"{self._inline(record['timestamp'], 80)} · "
                    f"{self._inline(record['detail'], _MAX_REASON_CHARS)}"
                )
        else:
            lines.append("- No ACCEPT records")

        lines.extend(["", "## Pause / Resume Events"])
        pause_resume = [row for row in event_rows if row.get("event_type") in self._PAUSE_RESUME_EVENT_TYPES]
        if pause_resume:
            for row in pause_resume:
                detail = self._event_detail(row)
                lines.append(
                    f"- `{self._inline(row.get('event_type'), 100)}` · "
                    f"{self._inline(row.get('timestamp'), 80)} · {detail}"
                )
        else:
            lines.append("- No pause/resume events")

        lines.extend([
            "",
            "## Safety",
            f"- Circuit: `{self._inline(safety.get('circuitState') or 'UNKNOWN', 80)}`",
            f"- Dispatch stopped: `{bool(safety.get('stopDispatch'))}`",
            f"- Workspace write protected: `{bool(safety.get('workspaceWriteProtected'))}`",
            f"- Trigger: {self._inline(safety.get('triggerAgentId') or '—', 180)}",
            f"- Trigger reason: {self._inline(safety.get('triggerReason') or '—', _MAX_REASON_CHARS)}",
            "",
            "### Safety events",
        ])
        safety_events = [row for row in event_rows if row.get("event_type") in self._SAFETY_EVENT_TYPES]
        if safety_events:
            for row in safety_events:
                lines.append(
                    f"- `{self._inline(row.get('event_type'), 100)}` · "
                    f"{self._inline(row.get('timestamp'), 80)} · {self._event_detail(row)}"
                )
        else:
            lines.append("- No persisted safety events")

        lines.extend(["", "## Audit Timeline Summary"])
        if event_rows:
            for row in event_rows:
                lines.append(
                    f"- {self._inline(row.get('timestamp'), 80)} · "
                    f"`{self._inline(row.get('event_type'), 120)}` · "
                    f"{self._inline(row.get('source'), 120)}"
                )
        else:
            lines.append("- No audit events")

        return MeetingSummaryDocument("\n".join(lines).rstrip() + "\n", self._redacted_field_count)

    @staticmethod
    def _value(value: Any) -> str:
        return str(getattr(value, "value", value) or "UNKNOWN")

    @staticmethod
    def _completion_state(status: Any) -> str:
        value = MeetingSummaryExporter._value(status)
        if value == "COMPLETED":
            return "COMPLETED"
        if value == "FAILED":
            return "FAILED"
        return f"NOT_COMPLETED ({value})"

    @staticmethod
    def _sorted_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return sorted((dict(row) for row in rows), key=lambda row: (str(row.get("timestamp") or row.get("createdAt") or ""), str(row.get("event_id") or row.get("eventId") or row.get("decisionId") or "")))

    def _inline(self, value: Any, limit: int) -> str:
        text = " ".join(str(value or "").split())
        if self._is_sensitive(text):
            self._redacted_field_count += 1
            return _REDACTED
        if len(text) > limit:
            return text[: limit - 1].rstrip() + "…"
        return text or "—"

    def _is_sensitive(self, text: str) -> bool:
        # Reuse the formal Brain Packet guard.  It is intentionally checked
        # before truncation so a secret cannot be hidden beyond the preview.
        return any(pattern.search(text) for pattern in ManualGPTBrainTransport._SENSITIVE_PATTERNS)

    def _event_detail(self, row: Mapping[str, Any]) -> str:
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, json.JSONDecodeError):
                payload = {}
        if not isinstance(payload, Mapping):
            return "recorded"
        pieces: list[str] = []
        for key in ("from", "to", "taskId", "agentId", "triggerAgentId", "participantType"):
            if payload.get(key) is not None:
                pieces.append(f"{key}={self._inline(payload[key], 180)}")
        if payload.get("reason"):
            pieces.append(f"reason={self._inline(payload['reason'], _MAX_REASON_CHARS)}")
        return "; ".join(pieces) or "recorded"

    @staticmethod
    def _accept_records(
        events: Iterable[Mapping[str, Any]],
        decisions: Iterable[Mapping[str, Any]],
        tasks: Iterable[Any],
    ) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        event_task_ids: set[str] = set()
        for row in events:
            if row.get("event_type") != "TaskAccepted":
                continue
            payload = row.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, json.JSONDecodeError):
                    payload = {}
            payload = payload if isinstance(payload, Mapping) else {}
            task_id = str(payload.get("taskId") or "—")
            event_task_ids.add(task_id)
            records.append({
                "taskId": task_id,
                "timestamp": str(row.get("timestamp") or ""),
                "detail": "TaskEngine recorded ACCEPT",
            })
        for row in decisions:
            if str(row.get("type") or row.get("decision") or "").upper() != "ACCEPT":
                continue
            records.append({
                "taskId": str(row.get("relatedTaskId") or row.get("taskId") or "—"),
                "timestamp": str(row.get("timestamp") or row.get("createdAt") or ""),
                "detail": f"GPT Brain ACCEPT · reason={str(row.get('reason') or '—')}",
            })
        # TaskEngine's durable accepted bit is a useful fallback when an
        # older database contains the task mutation but not its event row.
        for task in tasks:
            task_id = str(getattr(task, "task_id", "—"))
            if bool(getattr(task, "accepted", False)) and task_id not in event_task_ids:
                records.append({
                    "taskId": task_id,
                    "timestamp": str(getattr(task, "completed_at", None) or ""),
                    "detail": "TaskEngine accepted state",
                })
        return sorted(records, key=lambda row: row["timestamp"])
