"""Configurable AgentHeartbeat for runtime liveness."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from ..events.bus import EventBus
from ..models import AgentHealth, DomainEvent


@dataclass
class HeartbeatRecord:
    agent_id: str
    provider: str
    status: AgentHealth
    last_seen_at: str
    last_output_at: str | None = None
    current_task_id: str | None = None
    runtime_generation: int = 0
    terminal_id: str | None = None
    last_seen_monotonic: float | None = None


class AgentHeartbeat:
    def __init__(self, meeting_id: str, timeout_seconds: float, events: EventBus, persist=None, *, clock: Callable[[], float] | None = None) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.meeting_id = meeting_id
        self.timeout_seconds = timeout_seconds
        self.events = events
        self.persist = persist
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._records: dict[str, HeartbeatRecord] = {}

    def beat(self, agent_id: str, provider: str, status: AgentHealth, current_task_id: str | None = None, at: str | None = None, *, runtime_generation: int = 0, terminal_id: str | None = None) -> HeartbeatRecord:
        now = at or datetime.now(timezone.utc).isoformat()
        with self._lock:
            previous = self._records.get(agent_id)
            if previous and runtime_generation < previous.runtime_generation:
                self.events.publish(DomainEvent.create(
                    "STALE_RUNTIME_EVENT_IGNORED", self.meeting_id, "AgentHeartbeat",
                    {"agentId": agent_id, "runtimeGeneration": runtime_generation,
                     "activeGeneration": previous.runtime_generation, "terminalId": terminal_id,
                     "provider": provider},
                ))
                return previous
            record = HeartbeatRecord(
                agent_id=agent_id,
                provider=provider,
                status=status,
                last_seen_at=now,
                last_output_at=previous.last_output_at if previous else None,
                current_task_id=current_task_id,
                runtime_generation=runtime_generation,
                terminal_id=terminal_id,
                last_seen_monotonic=self._clock(),
            )
            self._records[agent_id] = record
        if self.persist:
            self.persist(agent_id, record)
        self.events.publish(DomainEvent.create("AgentHeartbeatReceived", self.meeting_id, "AgentHeartbeat", {
            "agentId": agent_id,
            "provider": provider,
            "status": status.value,
            "lastSeenAt": now,
            "currentTaskId": current_task_id,
            "runtimeGeneration": runtime_generation,
            "terminalId": terminal_id,
        }))
        return record

    def output(self, agent_id: str, at: str | None = None) -> HeartbeatRecord:
        with self._lock:
            record = self._records[agent_id]
            record.last_output_at = at or datetime.now(timezone.utc).isoformat()
        if self.persist:
            self.persist(agent_id, record)
        return record

    def get(self, agent_id: str) -> HeartbeatRecord:
        with self._lock:
            return self._records[agent_id]

    def timed_out(self, now: str | None = None, *, grace_seconds: float = 0.0) -> tuple[str, ...]:
        if grace_seconds < 0:
            raise ValueError("grace_seconds must not be negative")
        threshold = self.timeout_seconds + grace_seconds
        with self._lock:
            records = tuple(self._records.items())
        if now is not None:
            current = datetime.fromisoformat(now)
            return tuple(
                agent_id for agent_id, record in records
                if (current - datetime.fromisoformat(record.last_seen_at)).total_seconds() > threshold
            )
        current_monotonic = self._clock()
        return tuple(
            agent_id for agent_id, record in records
            if record.last_seen_monotonic is None
            or current_monotonic - record.last_seen_monotonic > threshold
        )
