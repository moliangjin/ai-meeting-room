"""Coordinates formal AgentAdapters without provider-specific business logic."""

from __future__ import annotations

import threading
from dataclasses import replace

from ..agents.adapter import AgentAdapter
from ..agents.registry import AgentRegistry
from ..events.bus import EventBus
from ..models import AgentHealth, AgentStatus, DomainEvent, RuntimeBinding, utc_now


class RuntimeCoordinator:
    def __init__(self, registry: AgentRegistry, events: EventBus) -> None:
        self.registry = registry
        self.events = events
        self.adapters: dict[str, AgentAdapter] = {}
        self.bindings: dict[str, RuntimeBinding] = {}
        self.retired_bindings: dict[str, list[RuntimeBinding]] = {}
        self._lock = threading.RLock()

    def register(self, agent_id: str, adapter: AgentAdapter) -> None:
        with self._lock:
            if agent_id in self.adapters:
                raise ValueError(f"adapter already registered: {agent_id}")
            # A restored domain Agent has no live binding.  Re-attaching a new
            # runtime must advance the durable generation so events from the
            # stopped pre-restart runtime can never be accepted as current.
            generation = max(self.registry.get(agent_id).runtime_generation + 1, 1)
            binding = self._make_binding(agent_id, adapter, generation)
            self.adapters[agent_id] = adapter
            self.bindings[agent_id] = binding
            self.events.publish(DomainEvent.create(
                "RuntimeBound", self.registry.meeting_id, "RuntimeCoordinator",
                self._binding_payload(binding),
            ))

    def replace(self, agent_id: str, adapter: AgentAdapter) -> None:
        """Compatibility alias for the generation-aware replacement path."""
        self.replace_runtime(agent_id, adapter)

    def replace_runtime(self, agent_id: str, adapter: AgentAdapter, *, publish_events: bool = True) -> RuntimeBinding:
        """Atomically switch one domain Agent to a ready, healthy runtime.

        The old binding remains in ``retired_bindings`` for audit purposes and
        can never become current again.  A replacement is not admitted until
        its adapter performs a successful health check.
        """
        with self._lock:
            if agent_id not in self.adapters:
                raise KeyError(agent_id)
            if not adapter.health_check():
                raise RuntimeError(f"replacement runtime is not healthy: {agent_id}")
            old = self.bindings[agent_id]
            retired = replace(old, state="RETIRED")
            binding = self._make_binding(agent_id, adapter, old.generation + 1)
            self.adapters[agent_id] = adapter
            self.bindings[agent_id] = binding
            self.retired_bindings.setdefault(agent_id, []).append(retired)
            if publish_events:
                self.publish_replacement_events(agent_id)
            return binding

    def publish_replacement_events(self, agent_id: str) -> None:
        """Publish replacement events after all domain maps are updated."""
        with self._lock:
            retired = self.retired_bindings[agent_id][-1]
            binding = self.bindings[agent_id]
            self.events.publish(DomainEvent.create(
                "RuntimeRetired", self.registry.meeting_id, "RuntimeCoordinator",
                self._binding_payload(retired),
            ))
            self.events.publish(DomainEvent.create(
                "RuntimeBound", self.registry.meeting_id, "RuntimeCoordinator",
                self._binding_payload(binding),
            ))

    @staticmethod
    def _runtime(adapter: AgentAdapter):
        return getattr(adapter, "runtime", adapter)

    def _make_binding(self, agent_id: str, adapter: AgentAdapter, generation: int) -> RuntimeBinding:
        runtime = self._runtime(adapter)
        runtime_id = str(getattr(runtime, "agent_id", None) or getattr(adapter, "agent_id", None) or getattr(runtime, "terminal_id", ""))
        if not runtime_id:
            raise RuntimeError(f"runtime has no stable id: {agent_id}")
        terminal_id = getattr(runtime, "terminal_id", None)
        session_id = getattr(runtime, "session_id", None)
        return RuntimeBinding(
            agent_id=agent_id,
            runtime_id=runtime_id,
            provider=self.registry.get(agent_id).provider,
            session_name=str(session_id) if session_id else None,
            session_id=str(session_id) if session_id else None,
            terminal_id=str(terminal_id) if terminal_id else None,
            generation=generation,
            bound_at=utc_now(),
        )

    @staticmethod
    def _binding_payload(binding: RuntimeBinding) -> dict[str, object]:
        return {
            "agentId": binding.agent_id,
            "runtimeId": binding.runtime_id,
            "provider": binding.provider,
            "sessionName": binding.session_name,
            "sessionId": binding.session_id,
            "terminalId": binding.terminal_id,
            "generation": binding.generation,
            "boundAt": binding.bound_at,
            "state": binding.state,
        }

    def current_binding(self, agent_id: str) -> RuntimeBinding:
        with self._lock:
            return self.bindings[agent_id]

    def is_current(self, agent_id: str, generation: int, adapter: AgentAdapter | None = None) -> bool:
        with self._lock:
            return (
                agent_id in self.bindings
                and self.bindings[agent_id].state == "ACTIVE"
                and self.bindings[agent_id].generation == generation
                and (adapter is None or self.adapters.get(agent_id) is adapter)
            )

    def all_bindings(self) -> tuple[RuntimeBinding, ...]:
        with self._lock:
            return tuple(self.bindings.values())

    def start_agent(self, agent_id: str) -> str:
        with self._lock:
            adapter = self.adapters[agent_id]
        runtime_id = adapter.start()
        self.registry.update(agent_id, status=AgentStatus.IDLE, health=AgentHealth.HEALTHY)
        self.events.publish(DomainEvent.create("AgentStarted", self.registry.meeting_id, "RuntimeCoordinator", {"agentId": agent_id, "runtimeId": runtime_id}))
        return runtime_id

    def stop_agent(self, agent_id: str) -> None:
        with self._lock:
            adapter = self.adapters[agent_id]
        adapter.stop()
        self.registry.update(agent_id, status=AgentStatus.STOPPED)
        self.events.publish(DomainEvent.create("AgentStopped", self.registry.meeting_id, "RuntimeCoordinator", {"agentId": agent_id}))

    def interrupt_agent(self, agent_id: str) -> None:
        with self._lock:
            adapter = self.adapters[agent_id]
        adapter.interrupt()

    def poll_agent(self, agent_id: str) -> AgentHealth:
        with self._lock:
            adapter = self.adapters[agent_id]
            binding = self.bindings[agent_id]
        try:
            status = adapter.get_status()
            health = AgentHealth.HEALTHY if adapter.health_check() else AgentHealth.UNKNOWN
            status_map = {"WORKING": AgentStatus.WORKING, "WAITING": AgentStatus.WAITING, "IDLE": AgentStatus.IDLE, "ERROR": AgentStatus.ERROR, "LOST": AgentStatus.LOST, "UNKNOWN": AgentStatus.UNKNOWN}
            if status == "ERROR":
                health = AgentHealth.ERROR
            elif status == "LOST":
                health = AgentHealth.LOST
            if not self.is_current(agent_id, binding.generation, adapter):
                self.events.publish(DomainEvent.create(
                    "STALE_RUNTIME_EVENT_IGNORED", self.registry.meeting_id, "RuntimeCoordinator",
                    {"agentId": agent_id, "runtimeGeneration": binding.generation,
                     "activeGeneration": self.current_binding(agent_id).generation,
                     "terminalId": binding.terminal_id},
                ))
                return self.registry.get(agent_id).health
            self.registry.update(agent_id, status=status_map.get(status, AgentStatus.UNKNOWN), health=health)
            return health
        except Exception as exc:
            if not self.is_current(agent_id, binding.generation, adapter):
                self.events.publish(DomainEvent.create(
                    "STALE_RUNTIME_EVENT_IGNORED", self.registry.meeting_id, "RuntimeCoordinator",
                    {"agentId": agent_id, "runtimeGeneration": binding.generation,
                     "activeGeneration": self.current_binding(agent_id).generation,
                     "terminalId": binding.terminal_id},
                ))
                return self.registry.get(agent_id).health
            self.registry.update(agent_id, status=AgentStatus.UNKNOWN, health=AgentHealth.UNKNOWN)
            self.events.publish(DomainEvent.create("AgentHealthChanged", self.registry.meeting_id, "RuntimeCoordinator", {"agentId": agent_id, "error": type(exc).__name__}))
            return AgentHealth.UNKNOWN

    def health_check(self, agent_id: str) -> bool:
        try:
            with self._lock:
                adapter = self.adapters[agent_id]
            return adapter.health_check()
        except Exception:
            return False
