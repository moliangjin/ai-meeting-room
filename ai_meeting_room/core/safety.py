"""Formal fail-closed safety engine and global circuit breaker."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Mapping

from ..events.bus import EventBus
from ..models import AgentHealth, AgentStatus, DomainEvent, MeetingStatus, utc_now


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    RECOVERING = "RECOVERING"


class DispatchBlockedError(RuntimeError):
    pass


@dataclass(frozen=True)
class CircuitEvent:
    event: str
    trigger_agent_id: str
    trigger_reason: str
    trigger_timestamp: str
    interrupted_agent_ids: tuple[str, ...]
    interrupt_errors: Mapping[str, str]
    participant_type: str = "AGENT"
    brain_participant_id: str | None = None
    brain_runtime_identity: Mapping[str, object] | None = None


class GlobalCircuitBreaker:
    _FAULTS = {AgentHealth.ERROR, AgentHealth.LOST, AgentHealth.UNKNOWN}

    def __init__(self, active_agent_ids: Callable[[], Iterable[str]], interrupt_agent: Callable[[str], None], persist_snapshot: Callable[[dict[str, object]], None] | None = None) -> None:
        self._active_agent_ids = active_agent_ids
        self._interrupt_agent = interrupt_agent
        self._persist_snapshot = persist_snapshot
        self.state = CircuitState.CLOSED
        self.meeting_paused = False
        self.workspace_write_protected = False
        self.trigger_agent_id: str | None = None
        self.trigger_reason: str | None = None
        self.trigger_timestamp: str | None = None
        self.last_event: CircuitEvent | None = None
        self.participant_type = "AGENT"
        self.brain_participant_id: str | None = None
        self.brain_runtime_identity: dict[str, object] | None = None

    @property
    def stop_dispatch(self) -> bool:
        return self.state != CircuitState.CLOSED

    def require_dispatch_allowed(self) -> None:
        if self.stop_dispatch:
            raise DispatchBlockedError(f"dispatch blocked while circuit={self.state.value}")

    def observe(self, agent_id: str, health: AgentHealth, reason: str) -> bool:
        if health in self._FAULTS:
            self.open(agent_id, reason)
            return True
        return False

    def open(
        self,
        agent_id: str,
        reason: str,
        *,
        participant_type: str = "AGENT",
        brain_participant_id: str | None = None,
        brain_runtime_identity: Mapping[str, object] | None = None,
    ) -> CircuitEvent:
        timestamp = utc_now()
        self.state = CircuitState.OPEN
        self.meeting_paused = True
        self.workspace_write_protected = True
        self.trigger_agent_id = agent_id
        self.trigger_reason = reason
        self.trigger_timestamp = timestamp
        self.participant_type = participant_type
        self.brain_participant_id = brain_participant_id if participant_type == "BRAIN" else None
        self.brain_runtime_identity = (
            {str(key): value for key, value in brain_runtime_identity.items() if isinstance(value, (str, int, bool)) or value is None}
            if participant_type == "BRAIN" and brain_runtime_identity is not None
            else None
        )
        try:
            active_ids = tuple(self._active_agent_ids())
        except BaseException:
            active_ids = ()
        interrupt_errors: dict[str, str] = {}
        for active_id in active_ids:
            try:
                self._interrupt_agent(active_id)
            except BaseException as exc:
                interrupt_errors[active_id] = f"{type(exc).__name__}: {exc}"
        self.last_event = CircuitEvent(
            "GLOBAL_PAUSE", agent_id, reason, timestamp, active_ids, interrupt_errors,
            participant_type, self.brain_participant_id, self.brain_runtime_identity,
        )
        self._persist()
        return self.last_event

    def begin_recovery(self) -> None:
        if self.state != CircuitState.OPEN:
            raise RuntimeError(f"cannot recover from {self.state.value}")
        self.state = CircuitState.RECOVERING
        self._persist()

    def resume(self, health_by_agent: Mapping[str, AgentHealth]) -> None:
        if self.state != CircuitState.RECOVERING:
            raise RuntimeError("resume requires RECOVERING")
        if not health_by_agent or any(value != AgentHealth.HEALTHY for value in health_by_agent.values()):
            raise ValueError("resume requires explicit HEALTHY status for every agent")
        self.state = CircuitState.CLOSED
        self.meeting_paused = False
        self.workspace_write_protected = False
        self._persist()

    def restore_snapshot(self, snapshot: Mapping[str, object]) -> None:
        """Restore persisted safety state without performing runtime actions."""
        raw_state = str(snapshot.get("circuitState", CircuitState.OPEN.value))
        self.state = CircuitState(raw_state)
        self.meeting_paused = bool(snapshot.get("meetingState") == "PAUSED" or self.state != CircuitState.CLOSED)
        self.workspace_write_protected = bool(snapshot.get("workspaceWriteProtected", self.state != CircuitState.CLOSED))
        self.trigger_agent_id = snapshot.get("triggerAgentId") if isinstance(snapshot.get("triggerAgentId"), str) else None
        self.trigger_reason = snapshot.get("triggerReason") if isinstance(snapshot.get("triggerReason"), str) else None
        self.trigger_timestamp = snapshot.get("triggerTimestamp") if isinstance(snapshot.get("triggerTimestamp"), str) else None
        if self.trigger_agent_id and self.trigger_reason and self.trigger_timestamp:
            participant_id = snapshot.get("brainParticipantId")
            self.brain_participant_id = participant_id if isinstance(participant_id, str) else None
            raw_identity = snapshot.get("brainRuntimeIdentity")
            allowed_identity_fields = {
                "registryInstanceId", "hostInstanceId", "boundPageId", "ownerThreadId",
                "processPid", "formalRuntimeType", "runtimeType", "runtimeInstanceId",
            }
            self.brain_runtime_identity = (
                {str(key): value for key, value in raw_identity.items() if key in allowed_identity_fields and (isinstance(value, (str, int, bool)) or value is None)}
                if isinstance(raw_identity, Mapping)
                else None
            )
            self.last_event = CircuitEvent(
                "GLOBAL_PAUSE", self.trigger_agent_id, self.trigger_reason, self.trigger_timestamp,
                tuple(str(item) for item in snapshot.get("interruptedAgentIds", []) if isinstance(item, str)),
                {str(key): str(value) for key, value in dict(snapshot.get("interruptErrors", {})).items()},
                str(snapshot.get("participantType") or "AGENT"),
                self.brain_participant_id,
                self.brain_runtime_identity,
            )
            self.participant_type = self.last_event.participant_type

    def snapshot(self) -> dict[str, object]:
        result: dict[str, object] = {
            "circuitState": self.state.value,
            "meetingState": "PAUSED" if self.meeting_paused else "RUNNING",
            "stopDispatch": self.stop_dispatch,
            "workspaceWriteProtected": self.workspace_write_protected,
            "triggerAgentId": self.trigger_agent_id,
            "triggerReason": self.trigger_reason,
            "triggerTimestamp": self.trigger_timestamp,
            "participantType": self.participant_type,
            "interruptedAgentIds": list(self.last_event.interrupted_agent_ids) if self.last_event else [],
            "interruptErrors": dict(self.last_event.interrupt_errors) if self.last_event else {},
        }
        if self.participant_type == "BRAIN":
            result["brainParticipantId"] = self.brain_participant_id
            result["brainRuntimeIdentity"] = dict(self.brain_runtime_identity or {})
        return result

    def _persist(self) -> None:
        if self._persist_snapshot:
            self._persist_snapshot(self.snapshot())

    def persist(self) -> None:
        """Persist the current fail-closed snapshot after restoration/repair."""
        self._persist()


class SafetyEngine:
    def __init__(self, meeting, registry, events: EventBus, interrupt_agent: Callable[[str], None], persist_snapshot: Callable[[dict[str, object]], None] | None = None, runtime_health_check: Callable[[str], AgentHealth] | None = None) -> None:
        self.meeting = meeting
        self.registry = registry
        self.events = events
        self.breaker = GlobalCircuitBreaker(registry.active_ids, interrupt_agent, persist_snapshot)
        self._runtime_health_check = runtime_health_check

    def require_dispatch_allowed(self) -> None:
        self.breaker.require_dispatch_allowed()

    def require_task_advancement_allowed(self) -> None:
        """Fail closed before any task lifecycle write or provider dispatch."""
        blocked_meetings = {
            MeetingStatus.PAUSED,
            MeetingStatus.RECOVERING,
            MeetingStatus.COMPLETED,
            MeetingStatus.FAILED,
        }
        if self.breaker.state != CircuitState.CLOSED:
            raise DispatchBlockedError(f"task advancement blocked while circuit={self.breaker.state.value}")
        if self.meeting.meeting.status in blocked_meetings:
            raise DispatchBlockedError(f"task advancement blocked while meeting={self.meeting.meeting.status.value}")

        unsafe_health = {AgentHealth.ERROR, AgentHealth.LOST, AgentHealth.UNKNOWN}
        unsafe_status = {
            AgentStatus.STARTING,
            AgentStatus.STOPPING,
            AgentStatus.STOPPED,
            AgentStatus.ERROR,
            AgentStatus.LOST,
            AgentStatus.UNKNOWN,
        }
        for agent in self.registry.all():
            if (
                agent.health in unsafe_health
                or agent.status in unsafe_status
                or agent.runtime_state != "ACTIVE"
            ):
                reason = (
                    f"task advancement blocked by unsafe participant/runtime: "
                    f"health={agent.health.value}, status={agent.status.value}, runtime={agent.runtime_state}"
                )
                self.observe_agent_failure(agent.agent_id, reason, agent.health if agent.health in unsafe_health else AgentHealth.UNKNOWN)
                raise DispatchBlockedError(reason)
            try:
                health = self._runtime_health_check(agent.agent_id) if self._runtime_health_check else agent.health
            except Exception as exc:
                reason = f"task advancement runtime health check failed: {type(exc).__name__}"
                self.observe_agent_failure(agent.agent_id, reason, AgentHealth.UNKNOWN)
                raise DispatchBlockedError(reason) from exc

            current = self.registry.get(agent.agent_id)
            if (
                health in unsafe_health
                or current.health in unsafe_health
                or current.status in unsafe_status
                or current.runtime_state != "ACTIVE"
            ):
                reason = (
                    f"task advancement blocked by unsafe participant/runtime: "
                    f"health={health.value}, status={current.status.value}, runtime={current.runtime_state}"
                )
                self.observe_agent_failure(agent.agent_id, reason, health if health in unsafe_health else AgentHealth.UNKNOWN)
                raise DispatchBlockedError(reason)

            if self.breaker.state != CircuitState.CLOSED or self.meeting.meeting.status in blocked_meetings:
                raise DispatchBlockedError("task advancement blocked after runtime health check")

    def observe_agent_failure(self, agent_id: str, reason: str, health: AgentHealth = AgentHealth.UNKNOWN) -> None:
        opened = self.breaker.observe(agent_id, health, reason)
        if not opened:
            return
        self.registry.update(agent_id, health=health)
        if self.meeting.meeting.status == MeetingStatus.RUNNING:
            self.meeting.transition(MeetingStatus.PAUSED, reason=reason, triggered_by=agent_id)
        self.events.publish(DomainEvent.create("CircuitBreakerOpened", self.meeting.meeting.meeting_id, "SafetyEngine", self.breaker.snapshot()))

    def observe_brain_failure(
        self,
        brain_id: str,
        reason: str,
        *,
        brain_participant_id: str | None = None,
        runtime_identity: Mapping[str, object] | None = None,
    ) -> None:
        """Open the same fail-closed circuit for a Brain outage.

        The browser bridge is a participant, but it is not an Agent and is
        therefore not inserted into the AgentRegistry or interrupted through
        the Agent runtime callback.
        """
        self.breaker.open(
            brain_id,
            reason,
            participant_type="BRAIN",
            brain_participant_id=brain_participant_id,
            brain_runtime_identity=runtime_identity,
        )
        if self.meeting.meeting.status == MeetingStatus.RUNNING:
            self.meeting.transition(MeetingStatus.PAUSED, reason=reason, triggered_by=brain_participant_id or brain_id)
        self.events.publish(DomainEvent.create("BrainCircuitBreakerOpened", self.meeting.meeting.meeting_id, "SafetyEngine", self.breaker.snapshot()))

    def observe_system_failure(self, system_id: str, reason: str) -> None:
        """Fail closed through the shared breaker when a safety monitor dies."""
        try:
            self.breaker.open(system_id, reason, participant_type="SYSTEM")
        except BaseException:
            # ``open`` establishes OPEN/write-protected state before any
            # interrupt or persistence side effect, so retain that state even
            # if a downstream callback fails.
            self.breaker.state = CircuitState.OPEN
            self.breaker.meeting_paused = True
            self.breaker.workspace_write_protected = True
            self.breaker.trigger_agent_id = system_id
            self.breaker.trigger_reason = reason
            self.breaker.trigger_timestamp = self.breaker.trigger_timestamp or utc_now()
            self.breaker.participant_type = "SYSTEM"
        if self.meeting.meeting.status in {MeetingStatus.RUNNING, MeetingStatus.RECOVERING, MeetingStatus.READY}:
            try:
                self.meeting.transition(MeetingStatus.PAUSED, reason=reason, triggered_by=system_id)
            except BaseException:
                # MeetingStateMachine assigns the PAUSED record before event
                # subscribers run; preserve the fail-closed exception boundary.
                pass
        try:
            self.events.publish(DomainEvent.create("SafetyMonitorFailed", self.meeting.meeting.meeting_id, "SafetyEngine", self.breaker.snapshot()))
        except BaseException:
            pass
