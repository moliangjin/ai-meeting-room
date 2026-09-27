"""Explicit all-components recovery coordinator."""

from __future__ import annotations

from ..models import AgentHealth, AgentStatus, MeetingStatus
from .errors import MeetingLifecycleConflict
from .safety import CircuitState


class RecoveryManager:
    def __init__(self, meeting, registry, safety, adapters, *, cao_health_check, workspace_health_check, persistence_health_check, brain_health_check=lambda: True) -> None:
        self.meeting = meeting
        self.registry = registry
        self.safety = safety
        self.adapters = adapters
        self.cao_health_check = cao_health_check
        self.workspace_health_check = workspace_health_check
        self.persistence_health_check = persistence_health_check
        self.brain_health_check = brain_health_check

    def recover(self) -> bool:
        if self.meeting.meeting.status != MeetingStatus.PAUSED:
            raise MeetingLifecycleConflict(
                "MEETING_NOT_PAUSED",
                f"recovery requires PAUSED meeting, got {self.meeting.meeting.status.value}",
            )
        self.meeting.transition(MeetingStatus.RECOVERING, reason="explicit operator recovery")
        health: dict[str, AgentHealth] = {}
        try:
            for agent in self.registry.all():
                adapter = self.adapters[agent.agent_id]
                if not adapter.health_check():
                    raise RuntimeError(f"agent health check failed: {agent.agent_id}")
                self.registry.update(agent.agent_id, health=AgentHealth.HEALTHY, status=AgentStatus.IDLE)
                health[agent.agent_id] = AgentHealth.HEALTHY
            if not self.cao_health_check() or not self.workspace_health_check() or not self.persistence_health_check() or not self.brain_health_check():
                raise RuntimeError("CAO, workspace, persistence or Brain health check failed")
            self.safety.breaker.begin_recovery()
            self.safety.breaker.resume(health)
            self.meeting.transition(MeetingStatus.READY, reason="all health checks passed")
            self.meeting.transition(MeetingStatus.RUNNING, reason="controlled resume")
            return True
        except Exception as exc:
            # Recovery is fail-closed even when an exception occurs after the
            # breaker has already moved toward CLOSED.  This is important for
            # persistence/event failures during the final resume transitions:
            # PAUSED must never be durable alongside a CLOSED breaker.
            reason = f"recovery blocked: {type(exc).__name__}"
            if self.safety.breaker.state != CircuitState.OPEN:
                try:
                    self.safety.breaker.open("recovery", reason)
                except Exception:
                    # ``open`` is best-effort for interrupt/persistence
                    # actions, but its in-memory state is fail-closed before
                    # those actions are attempted.
                    self.safety.breaker.state = CircuitState.OPEN
                    self.safety.breaker.meeting_paused = True
                    self.safety.breaker.workspace_write_protected = True
            if self.meeting.meeting.status in {MeetingStatus.RECOVERING, MeetingStatus.READY, MeetingStatus.RUNNING}:
                self.meeting.transition(MeetingStatus.PAUSED, reason=reason, triggered_by="recovery")
            return False
