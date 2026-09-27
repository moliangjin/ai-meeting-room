"""AgentRegistry owns Meeting membership and agent state changes."""

from __future__ import annotations

from dataclasses import replace

from ..events.bus import EventBus
from ..models import AgentHealth, AgentRecord, AgentStatus, DomainEvent


class AgentRegistry:
    def __init__(self, meeting_id: str, events: EventBus, persist_agent=None) -> None:
        self.meeting_id = meeting_id
        self.events = events
        self._persist_agent = persist_agent
        self._agents: dict[str, AgentRecord] = {}

    def add(self, agent: AgentRecord, *, emit_event: bool = True) -> AgentRecord:
        """Add an Agent, optionally hydrating it without a new business event.

        Normal admissions publish ``AgentAdded``.  Persistence restore is a
        state hydration operation, however, and must not manufacture a second
        membership event every time the Product Shell reloads.
        """
        if agent.agent_id in self._agents:
            raise ValueError(f"agent already exists: {agent.agent_id}")
        self._agents[agent.agent_id] = agent
        if self._persist_agent:
            self._persist_agent(agent)
        if emit_event:
            self.events.publish(DomainEvent.create("AgentAdded", self.meeting_id, "AgentRegistry", {"agentId": agent.agent_id, "provider": agent.provider}))
        return agent

    def remove(self, agent_id: str) -> None:
        self._agents.pop(agent_id)
        self.events.publish(DomainEvent.create("AgentRemoved", self.meeting_id, "AgentRegistry", {"agentId": agent_id}))

    def get(self, agent_id: str) -> AgentRecord:
        return self._agents[agent_id]

    def all(self) -> tuple[AgentRecord, ...]:
        return tuple(self._agents.values())

    def active_ids(self) -> tuple[str, ...]:
        # Only runtimes that can currently be doing work receive an interrupt.
        # The triggering runtime is observed before the registry is updated, so
        # it is still included when it was actually running.  Idle agents must
        # not receive a synthetic Ctrl-C: doing so can turn a healthy CAO
        # terminal into an error state and would make controlled recovery
        # impossible.
        active = {AgentStatus.STARTING, AgentStatus.WORKING, AgentStatus.WAITING, AgentStatus.STOPPING}
        return tuple(agent.agent_id for agent in self._agents.values() if agent.status in active)

    def update(self, agent_id: str, *, status: AgentStatus | None = None, health: AgentHealth | None = None, **fields: str | None) -> AgentRecord:
        old = self.get(agent_id)
        updated = replace(old, **({"status": status} if status else {}), **({"health": health} if health else {}), **fields)
        self._agents[agent_id] = updated
        if self._persist_agent:
            self._persist_agent(updated)
        if status and status != old.status:
            self.events.publish(DomainEvent.create("AgentStatusChanged", self.meeting_id, "AgentRegistry", {"agentId": agent_id, "from": old.status.value, "to": status.value}))
        if health and health != old.health:
            self.events.publish(DomainEvent.create("AgentHealthChanged", self.meeting_id, "AgentRegistry", {"agentId": agent_id, "from": old.health.value, "to": health.value}))
        return updated

    def all_explicitly_healthy(self) -> bool:
        return bool(self._agents) and all(agent.health == AgentHealth.HEALTHY for agent in self._agents.values())
