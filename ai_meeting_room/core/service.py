"""MeetingCore composition root for the Phase 1 foundation."""

from __future__ import annotations

import threading
from uuid import uuid4

from ..agents.registry import AgentRegistry
from ..core.heartbeat import AgentHeartbeat
from ..core.errors import MeetingLifecycleConflict
from ..events.bus import EventBus
from ..models import AgentHealth, AgentRecord, AgentStatus, DomainEvent, MeetingRecord, MeetingStatus, TaskRecord, TaskStatus, utc_now
from ..persistence.sqlite_store import SQLiteStore
from ..runtime.coordinator import RuntimeCoordinator
from ..tasks.engine import TaskEngine
from .meeting import MeetingStateMachine
from .recovery import RecoveryManager
from .safety import SafetyEngine


class MeetingCore:
    """Owns composition and lifecycle; adapters remain injected at the edge."""

    def __init__(self, meeting: MeetingStateMachine, store: SQLiteStore, *, heartbeat_timeout_seconds: float = 60.0) -> None:
        self.meeting = meeting
        self.store = store
        self.events = meeting.events
        self.registry = AgentRegistry(meeting.meeting.meeting_id, self.events, lambda agent: store.save_agent(meeting.meeting.meeting_id, agent))
        self.runtime = RuntimeCoordinator(self.registry, self.events)
        self.safety = SafetyEngine(
            meeting,
            self.registry,
            self.events,
            self.runtime.interrupt_agent,
            lambda snapshot: store.save_circuit(meeting.meeting.meeting_id, snapshot),
            runtime_health_check=self.runtime.poll_agent,
        )
        self.tasks = TaskEngine(meeting.meeting.meeting_id, self.registry, self.safety, self.events, store.save_task)
        self.heartbeat = AgentHeartbeat(meeting.meeting.meeting_id, heartbeat_timeout_seconds, self.events, self._persist_heartbeat)
        self._lifecycle_lock = threading.RLock()
        self.events.subscribe("*", store.append_event)
        self._save_meeting()

    @classmethod
    def create(cls, name: str, store: SQLiteStore, meeting_id: str | None = None, workspace_id: str | None = None, *, heartbeat_timeout_seconds: float = 60.0) -> "MeetingCore":
        events = EventBus()
        machine = MeetingStateMachine(MeetingRecord(meeting_id or str(uuid4()), name, workspace_id=workspace_id), events)
        core = cls(machine, store, heartbeat_timeout_seconds=heartbeat_timeout_seconds)
        core.events.publish(DomainEvent.create("MeetingCreated", machine.meeting.meeting_id, "MeetingCore", {"name": name}))
        return core

    @classmethod
    def restore(cls, meeting_id: str, store: SQLiteStore, *, heartbeat_timeout_seconds: float = 60.0) -> "MeetingCore":
        from ..models import AgentHealth, AgentRole, AgentStatus, TaskStatus
        data = store.get_meeting(meeting_id)
        if not data:
            raise KeyError(meeting_id)
        prior_status = MeetingStatus(data["status"])
        meeting = MeetingRecord(**{**data, "status": prior_status})
        machine = MeetingStateMachine(meeting, EventBus())
        if meeting.status == MeetingStatus.RUNNING:
            machine.transition(MeetingStatus.PAUSED, reason="process restarted while meeting was RUNNING; health confirmation required", triggered_by="process-restart")
        elif meeting.status == MeetingStatus.RECOVERING:
            machine.transition(MeetingStatus.PAUSED, reason="process restarted during recovery; explicit health confirmation required", triggered_by="process-restart")
        core = cls(machine, store, heartbeat_timeout_seconds=heartbeat_timeout_seconds)
        for raw in store.list_agents(meeting_id):
            raw["role"] = AgentRole(raw["role"])
            raw["status"] = AgentStatus(raw["status"])
            raw["health"] = AgentHealth(raw["health"])
            # Restore hydrates the registry; it must not republish an
            # AgentAdded business event for an already-persisted member.
            core.registry.add(AgentRecord(**raw), emit_event=False)
        for raw in store.list_tasks(meeting_id):
            raw["status"] = TaskStatus(raw["status"])
            core.tasks._tasks[raw["task_id"]] = TaskRecord(**raw)
        persisted_circuit = None
        circuit_load_error = None
        try:
            persisted_circuit = store.get_circuit(meeting_id)
            if persisted_circuit is not None and not isinstance(persisted_circuit, dict):
                raise ValueError("circuit checkpoint is not an object")
        except Exception as exc:
            # A corrupt safety checkpoint is data, not permission to default
            # to CLOSED.  Keep only the exception class; persisted payloads
            # may contain operator-provided text.
            circuit_load_error = type(exc).__name__

        requires_recovery = prior_status in {
            MeetingStatus.RUNNING,
            MeetingStatus.PAUSED,
            MeetingStatus.RECOVERING,
        }
        if requires_recovery:
            snapshot = dict(persisted_circuit or {})
            persisted_state = snapshot.get("circuitState")
            if prior_status == MeetingStatus.RUNNING:
                trigger_reason = "process restarted while meeting was RUNNING; health confirmation required"
                trigger_agent_id = "process-restart"
            elif prior_status == MeetingStatus.RECOVERING or persisted_state == "RECOVERING":
                trigger_reason = "process restarted during recovery; explicit health confirmation required"
                trigger_agent_id = "process-restart"
            elif circuit_load_error:
                trigger_reason = f"persisted circuit checkpoint corrupt ({circuit_load_error}); explicit recovery required"
                trigger_agent_id = "process-restore"
            elif persisted_circuit is None:
                trigger_reason = "persisted circuit checkpoint missing for PAUSED meeting; explicit recovery required"
                trigger_agent_id = "process-restore"
            elif persisted_state != "OPEN":
                trigger_reason = "inconsistent persisted PAUSED/circuit checkpoint; explicit recovery required"
                trigger_agent_id = "process-restore"
            else:
                trigger_reason = str(snapshot.get("triggerReason") or meeting.pause_reason or "paused meeting requires explicit health confirmation")
                trigger_agent_id = str(snapshot.get("triggerAgentId") or meeting.pause_triggered_by or "process-restore")

            participant_type = snapshot.get("participantType")
            if participant_type not in {"AGENT", "BRAIN", "SYSTEM"}:
                participant_type = "AGENT"
            interrupted = snapshot.get("interruptedAgentIds")
            interrupt_errors = snapshot.get("interruptErrors")
            normalized_snapshot = {
                "circuitState": "OPEN",
                "meetingState": "PAUSED",
                "stopDispatch": True,
                "workspaceWriteProtected": True,
                "triggerAgentId": trigger_agent_id,
                "triggerReason": trigger_reason,
                "triggerTimestamp": snapshot.get("triggerTimestamp") or meeting.paused_at or utc_now(),
                "participantType": participant_type,
                "interruptedAgentIds": [item for item in interrupted if isinstance(item, str)] if isinstance(interrupted, list) else [],
                "interruptErrors": {
                    str(key): str(value) for key, value in interrupt_errors.items()
                } if isinstance(interrupt_errors, dict) else {},
            }
            core.safety.breaker.restore_snapshot(normalized_snapshot)
            # Persist the normalized safety checkpoint before the restored
            # Core is published to callers or the Product API is opened.
            core.safety.breaker.persist()
        elif persisted_circuit:
            core.safety.breaker.restore_snapshot(persisted_circuit)
        core._save_meeting()
        return core

    def add_agent(self, agent: AgentRecord, adapter=None) -> AgentRecord:
        with self._lifecycle_lock:
            self.registry.add(agent)
            self.meeting.add_agent(agent.agent_id)
            if adapter is not None:
                self.runtime.register(agent.agent_id, adapter)
                binding = self.runtime.current_binding(agent.agent_id)
                self.tasks.register_adapter(agent.agent_id, adapter, generation=binding.generation)
                self.registry.update(
                    agent.agent_id,
                    session_id=binding.session_id,
                    terminal_id=binding.terminal_id,
                    runtime_id=binding.runtime_id,
                    runtime_generation=binding.generation,
                    runtime_bound_at=binding.bound_at,
                    runtime_state=binding.state,
                )
            self._save_meeting()
        return agent

    def create_task(self, title: str, instruction: str, *, task_id: str | None = None, parent_task_id: str | None = None):
        if self.meeting.meeting.status in {MeetingStatus.COMPLETED, MeetingStatus.FAILED}:
            raise RuntimeError(f"cannot create task for terminal meeting: {self.meeting.meeting.status.value}")
        task = self.tasks.create_task(title, instruction, task_id=task_id, parent_task_id=parent_task_id)
        self.meeting.add_task(task.task_id)
        self._save_meeting()
        return task

    def set_waiting_for_human_handoff(self, waiting: bool) -> None:
        """Gate task advancement while the user transports a Brain packet by hand."""
        self.tasks.set_human_handoff_pending(waiting)

    def beat_agent(self, agent_id: str, status, current_task_id: str | None = None, *, runtime_generation: int | None = None, terminal_id: str | None = None):
        agent = self.registry.get(agent_id)
        binding = self.runtime.current_binding(agent_id) if agent_id in self.runtime.bindings else None
        generation = binding.generation if runtime_generation is None and binding else (runtime_generation or 0)
        terminal = terminal_id if terminal_id is not None else (binding.terminal_id if binding else agent.terminal_id)
        record = self.heartbeat.beat(agent_id, agent.provider, status, current_task_id, runtime_generation=generation, terminal_id=terminal)
        if binding is None or generation >= binding.generation:
            self.registry.update(agent_id, last_heartbeat_at=record.last_seen_at)
        return record

    def poll_agent(self, agent_id: str):
        health = self.runtime.poll_agent(agent_id)
        if health in {AgentHealth.ERROR, AgentHealth.LOST, AgentHealth.UNKNOWN}:
            adapter = self.runtime.adapters.get(agent_id)
            detail = getattr(adapter, "runtime", adapter)
            reason = getattr(detail, "error", None) or f"runtime health={health.value}"
            self.safety.observe_agent_failure(agent_id, reason, health)
        else:
            binding = self.runtime.current_binding(agent_id)
            self.beat_agent(agent_id, health, self.registry.get(agent_id).current_task_id, runtime_generation=binding.generation, terminal_id=binding.terminal_id)
        return health

    def replace_runtime(self, agent_id: str, adapter) -> object:
        """Replace runtime, task mapping, registry and heartbeat target together."""
        with self._lifecycle_lock:
            binding = self.runtime.replace_runtime(agent_id, adapter, publish_events=False)
            self.tasks.register_adapter(agent_id, adapter, generation=binding.generation)
            self.registry.update(
                agent_id,
                session_id=binding.session_id,
                terminal_id=binding.terminal_id,
                runtime_id=binding.runtime_id,
                runtime_generation=binding.generation,
                runtime_bound_at=binding.bound_at,
                runtime_state="ACTIVE",
                status=AgentStatus.IDLE,
                health=AgentHealth.HEALTHY,
                current_task_id=None,
            )
            self.beat_agent(agent_id, AgentHealth.IDLE, runtime_generation=binding.generation, terminal_id=binding.terminal_id)
            self.runtime.publish_replacement_events(agent_id)
            return binding

    def mark_ready(self) -> None:
        self.meeting.transition(MeetingStatus.READY)
        self._save_meeting()

    def start(self) -> None:
        if not self.registry.all_explicitly_healthy():
            raise RuntimeError("all joined agents must be explicitly HEALTHY before start")
        self.meeting.transition(MeetingStatus.RUNNING)
        self._save_meeting()

    def pause(self, reason: str, triggered_by: str = "operator") -> None:
        with self._lifecycle_lock:
            if self.meeting.meeting.status != MeetingStatus.RUNNING:
                raise MeetingLifecycleConflict(
                    "MEETING_NOT_RUNNING",
                    f"pause requires RUNNING meeting, got {self.meeting.meeting.status.value}",
                )
            self.safety.breaker.open(triggered_by, reason)
            self.meeting.transition(MeetingStatus.PAUSED, reason=reason, triggered_by=triggered_by)
            self._save_meeting()

    def recover(self, *, cao_health_check, workspace_health_check, brain_health_check=lambda: True) -> bool:
        with self._lifecycle_lock:
            manager = RecoveryManager(self.meeting, self.registry, self.safety, self.runtime.adapters, cao_health_check=cao_health_check, workspace_health_check=workspace_health_check, persistence_health_check=self.store.health_check, brain_health_check=brain_health_check)
            result = manager.recover()
            if result:
                try:
                    for agent in self.registry.all():
                        binding = self.runtime.current_binding(agent.agent_id)
                        self.beat_agent(
                            agent.agent_id,
                            AgentHealth.IDLE,
                            agent.current_task_id,
                            runtime_generation=binding.generation,
                            terminal_id=binding.terminal_id,
                        )
                except Exception as exc:
                    self.safety.observe_system_failure("recovery-heartbeat-refresh", f"heartbeat refresh failed: {type(exc).__name__}")
                    result = False
            self._save_meeting()
            return result

    def complete_meeting(self, *, reason: str = "explicit COMPLETE_MEETING") -> None:
        if self.meeting.meeting.status != MeetingStatus.RUNNING:
            raise RuntimeError(f"COMPLETE_MEETING requires RUNNING meeting, got {self.meeting.meeting.status.value}")
        unfinished = {TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.DISPATCHED, TaskStatus.WORKING, TaskStatus.WAITING, TaskStatus.FAILED, TaskStatus.BLOCKED}
        if any(task.status in unfinished for task in self.tasks.all()):
            raise RuntimeError("COMPLETE_MEETING requires no active, failed, or blocked task")
        if self.safety.breaker.state.value != "CLOSED":
            raise RuntimeError("COMPLETE_MEETING requires CLOSED circuit")
        self.meeting.transition(MeetingStatus.COMPLETED, reason=reason, triggered_by="ManualBrain")
        self._save_meeting()

    def _save_meeting(self) -> None:
        self.store.save_meeting(self.meeting.meeting)

    def _persist_heartbeat(self, agent_id: str, record) -> None:
        self.store.save_heartbeat(self.meeting.meeting.meeting_id, agent_id, {
            "agentId": record.agent_id,
            "provider": record.provider,
            "status": record.status.value,
            "lastSeenAt": record.last_seen_at,
            "lastOutputAt": record.last_output_at,
            "currentTaskId": record.current_task_id,
            "runtimeGeneration": record.runtime_generation,
            "terminalId": record.terminal_id,
        })
