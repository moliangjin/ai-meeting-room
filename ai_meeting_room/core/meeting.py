"""Meeting entity and explicit state transitions."""

from __future__ import annotations

from dataclasses import replace

from ..events.bus import EventBus
from ..models import DomainEvent, MeetingRecord, MeetingStatus, utc_now


class InvalidMeetingTransition(RuntimeError):
    pass


class MeetingStateMachine:
    _TRANSITIONS = {
        MeetingStatus.CREATED: {MeetingStatus.READY, MeetingStatus.FAILED},
        # Recovery may fail after the READY transition has been applied but
        # before the controlled RUNNING transition completes.  PAUSED is the
        # only safe destination for that partial-resume state.
        MeetingStatus.READY: {MeetingStatus.RUNNING, MeetingStatus.PAUSED, MeetingStatus.FAILED},
        MeetingStatus.RUNNING: {MeetingStatus.PAUSED, MeetingStatus.COMPLETED, MeetingStatus.FAILED},
        MeetingStatus.PAUSED: {MeetingStatus.RECOVERING, MeetingStatus.FAILED},
        MeetingStatus.RECOVERING: {MeetingStatus.READY, MeetingStatus.RUNNING, MeetingStatus.PAUSED, MeetingStatus.FAILED},
        MeetingStatus.COMPLETED: set(),
        MeetingStatus.FAILED: set(),
    }

    def __init__(self, meeting: MeetingRecord, events: EventBus, source: str = "MeetingCore") -> None:
        self.meeting = meeting
        self.events = events
        self.source = source

    def transition(self, target: MeetingStatus, *, reason: str | None = None, triggered_by: str | None = None) -> MeetingRecord:
        current = self.meeting.status
        if target == current:
            return self.meeting
        if target not in self._TRANSITIONS[current]:
            raise InvalidMeetingTransition(f"{current.value} -> {target.value} is not allowed")
        now = utc_now()
        self.meeting = replace(
            self.meeting,
            status=target,
            started_at=now if target == MeetingStatus.RUNNING and self.meeting.started_at is None else self.meeting.started_at,
            paused_at=now if target == MeetingStatus.PAUSED else self.meeting.paused_at,
            pause_reason=reason if target == MeetingStatus.PAUSED else self.meeting.pause_reason,
            pause_triggered_by=triggered_by if target == MeetingStatus.PAUSED else self.meeting.pause_triggered_by,
            completed_at=now if target == MeetingStatus.COMPLETED else self.meeting.completed_at,
        )
        event_type = {
            MeetingStatus.PAUSED: "MeetingPaused",
            MeetingStatus.RECOVERING: "MeetingRecovering",
            MeetingStatus.READY: "MeetingReady",
            MeetingStatus.RUNNING: "MeetingStarted" if current != MeetingStatus.RECOVERING else "MeetingResumed",
            MeetingStatus.COMPLETED: "MeetingCompleted",
            MeetingStatus.FAILED: "MeetingFailed",
        }.get(target, "MeetingStatusChanged")
        self.events.publish(DomainEvent.create(event_type, self.meeting.meeting_id, self.source, {
            "from": current.value,
            "to": target.value,
            "reason": reason,
            "triggeredBy": triggered_by,
        }))
        return self.meeting

    def add_agent(self, agent_id: str) -> MeetingRecord:
        if agent_id not in self.meeting.agent_ids:
            self.meeting.agent_ids.append(agent_id)
            self.events.publish(DomainEvent.create("AgentAdded", self.meeting.meeting_id, self.source, {"agentId": agent_id}))
        return self.meeting

    def add_task(self, task_id: str) -> MeetingRecord:
        if task_id not in self.meeting.task_ids:
            self.meeting.task_ids.append(task_id)
            self.events.publish(DomainEvent.create("TaskCreated", self.meeting.meeting_id, self.source, {"taskId": task_id}))
        return self.meeting
