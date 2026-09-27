"""Minimal global circuit breaker and configurable heartbeat implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Iterable, Mapping


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    RECOVERING = "RECOVERING"


class MeetingState(str, Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"


class AgentHealth(str, Enum):
    HEALTHY = "HEALTHY"
    IDLE = "IDLE"
    WORKING = "WORKING"
    WAITING = "WAITING"
    ERROR = "ERROR"
    LOST = "LOST"
    UNKNOWN = "UNKNOWN"


class DispatchBlockedError(RuntimeError):
    """Raised when a task is attempted while the global safety gate is closed."""


@dataclass(frozen=True)
class CircuitEvent:
    event: str
    trigger_agent_id: str
    trigger_reason: str
    trigger_timestamp: str
    interrupted_agent_ids: tuple[str, ...]
    interrupt_errors: Mapping[str, str]


class GlobalCircuitBreaker:
    """Fail-closed meeting gate.

    ``active_agent_ids`` and ``interrupt_agent`` are injected callbacks so the
    breaker never knows whether an agent is backed by CAO, ACP, or another
    runtime. An interrupt failure is recorded and never treated as success.
    """

    _FAULTS = {AgentHealth.ERROR, AgentHealth.LOST, AgentHealth.UNKNOWN}

    def __init__(
        self,
        active_agent_ids: Callable[[], Iterable[str]],
        interrupt_agent: Callable[[str], None],
        clock: Callable[[], datetime] | None = None,
        persist_snapshot: Callable[[Mapping[str, object]], None] | None = None,
    ) -> None:
        self._active_agent_ids = active_agent_ids
        self._interrupt_agent = interrupt_agent
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._persist_snapshot = persist_snapshot
        self.state = CircuitState.CLOSED
        self.meeting_state = MeetingState.RUNNING
        self.workspace_write_protected = False
        self.trigger_agent_id: str | None = None
        self.trigger_reason: str | None = None
        self.trigger_timestamp: str | None = None
        self.last_event: CircuitEvent | None = None

    @property
    def stop_dispatch(self) -> bool:
        return self.state != CircuitState.CLOSED

    def require_dispatch_allowed(self) -> None:
        """Enforce the breaker before every new task dispatch."""
        if self.stop_dispatch:
            raise DispatchBlockedError(
                f"dispatch blocked while circuit={self.state.value}; meeting={self.meeting_state.value}"
            )

    def observe(self, agent_id: str, health: AgentHealth, reason: str | None = None) -> bool:
        """Open on ERROR/LOST/UNKNOWN; return whether the circuit opened."""
        if health in self._FAULTS:
            self.open(agent_id, reason or f"agent health={health.value}")
            return True
        return False

    def open(self, agent_id: str, reason: str) -> CircuitEvent:
        """Persist the trigger and attempt interruption of every active agent."""
        timestamp = self._clock().astimezone(timezone.utc).isoformat()
        active_ids = tuple(self._active_agent_ids())
        interrupt_errors: dict[str, str] = {}
        for active_id in active_ids:
            try:
                self._interrupt_agent(active_id)
            except Exception as exc:  # best effort, but never hidden
                interrupt_errors[active_id] = f"{type(exc).__name__}: {exc}"

        self.state = CircuitState.OPEN
        self.meeting_state = MeetingState.PAUSED
        self.workspace_write_protected = True
        self.trigger_agent_id = agent_id
        self.trigger_reason = reason
        self.trigger_timestamp = timestamp
        self.last_event = CircuitEvent(
            event="GLOBAL_PAUSE",
            trigger_agent_id=agent_id,
            trigger_reason=reason,
            trigger_timestamp=timestamp,
            interrupted_agent_ids=active_ids,
            interrupt_errors=interrupt_errors,
        )
        if self._persist_snapshot is not None:
            self._persist_snapshot(self.snapshot())
        return self.last_event

    def snapshot(self) -> dict[str, object]:
        """Return the state a durable Session/Log/Error/Task store should save."""
        return {
            "circuitState": self.state.value,
            "meetingState": self.meeting_state.value,
            "workspaceWriteProtected": self.workspace_write_protected,
            "triggerAgentId": self.trigger_agent_id,
            "triggerReason": self.trigger_reason,
            "triggerTimestamp": self.trigger_timestamp,
            "interruptedAgentIds": list(self.last_event.interrupted_agent_ids)
            if self.last_event
            else [],
            "interruptErrors": dict(self.last_event.interrupt_errors) if self.last_event else {},
        }

    def begin_recovery(self) -> None:
        """Move OPEN → RECOVERING; no dispatch is allowed in this state."""
        if self.state != CircuitState.OPEN:
            raise RuntimeError(f"cannot begin recovery from {self.state.value}")
        self.state = CircuitState.RECOVERING

    def resume(self, health_by_agent: Mapping[str, AgentHealth]) -> None:
        """Resume only after explicit HEALTHY confirmation for all agents.

        Missing agents and IDLE/WORKING/WAITING are rejected: operational
        status is not proof that a previously faulty runtime has recovered.
        """
        if self.state != CircuitState.RECOVERING:
            raise RuntimeError("resume requires RECOVERING state")
        if not health_by_agent:
            raise ValueError("resume requires explicit health for every agent")
        unsafe = {
            agent_id: health.value
            for agent_id, health in health_by_agent.items()
            if health != AgentHealth.HEALTHY
        }
        if unsafe:
            raise ValueError(f"resume blocked; agents not explicitly HEALTHY: {unsafe}")
        self.state = CircuitState.CLOSED
        self.meeting_state = MeetingState.RUNNING
        self.workspace_write_protected = False


@dataclass
class HeartbeatRecord:
    agent_id: str
    provider: str
    status: AgentHealth
    last_seen_at: datetime
    last_output_at: datetime | None = None
    current_task_id: str | None = None


class AgentHeartbeat:
    """In-memory POC store; production persistence belongs in Session/Log state."""

    def __init__(self, timeout_seconds: float, clock: Callable[[], datetime] | None = None) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._records: dict[str, HeartbeatRecord] = {}

    def beat(
        self,
        agent_id: str,
        provider: str,
        status: AgentHealth,
        current_task_id: str | None = None,
        at: datetime | None = None,
    ) -> HeartbeatRecord:
        now = (at or self._clock()).astimezone(timezone.utc)
        previous = self._records.get(agent_id)
        record = HeartbeatRecord(
            agent_id=agent_id,
            provider=provider,
            status=status,
            last_seen_at=now,
            last_output_at=previous.last_output_at if previous else None,
            current_task_id=current_task_id,
        )
        self._records[agent_id] = record
        return record

    def output(self, agent_id: str, at: datetime | None = None) -> HeartbeatRecord:
        if agent_id not in self._records:
            raise KeyError(agent_id)
        record = self._records[agent_id]
        record.last_output_at = (at or self._clock()).astimezone(timezone.utc)
        return record

    def get(self, agent_id: str) -> HeartbeatRecord:
        return self._records[agent_id]

    def timed_out(self, at: datetime | None = None) -> tuple[str, ...]:
        now = (at or self._clock()).astimezone(timezone.utc)
        return tuple(
            agent_id
            for agent_id, record in self._records.items()
            if (now - record.last_seen_at).total_seconds() > self.timeout_seconds
        )

    def snapshot(self) -> tuple[HeartbeatRecord, ...]:
        return tuple(self._records.values())
