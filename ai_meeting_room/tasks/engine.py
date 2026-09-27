"""Durable, safety-gated task lifecycle."""

from __future__ import annotations

import threading
from dataclasses import replace

from ..agents.adapter import AgentAdapter
from ..agents.registry import AgentRegistry
from ..core.safety import DispatchBlockedError
from ..events.bus import EventBus
from ..models import DomainEvent, TaskRecord, TaskStatus, utc_now


class InvalidTaskTransition(RuntimeError):
    pass


class HumanHandoffPendingError(RuntimeError):
    """Normal transport wait: block new work without opening the safety breaker."""


class TaskEngine:
    _TRANSITIONS = {
        TaskStatus.PENDING: {TaskStatus.QUEUED, TaskStatus.DISPATCHED, TaskStatus.CANCELLED, TaskStatus.BLOCKED},
        TaskStatus.QUEUED: {TaskStatus.DISPATCHED, TaskStatus.CANCELLED, TaskStatus.BLOCKED},
        TaskStatus.DISPATCHED: {TaskStatus.WORKING, TaskStatus.WAITING, TaskStatus.FAILED, TaskStatus.CANCELLED},
        TaskStatus.WORKING: {TaskStatus.WAITING, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED},
        TaskStatus.WAITING: {TaskStatus.WORKING, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED},
        TaskStatus.COMPLETED: set(), TaskStatus.FAILED: set(), TaskStatus.CANCELLED: set(), TaskStatus.BLOCKED: {TaskStatus.CANCELLED, TaskStatus.DISPATCHED},
    }

    def __init__(self, meeting_id: str, registry: AgentRegistry, safety, events: EventBus, persist_task=None) -> None:
        self.meeting_id = meeting_id
        self.registry = registry
        self.safety = safety
        self.events = events
        self._persist_task = persist_task
        self._tasks: dict[str, TaskRecord] = {}
        self._adapters: dict[str, AgentAdapter] = {}
        self._adapter_generations: dict[str, int] = {}
        self._dispatch_lock_guard = threading.Lock()
        self._agent_dispatch_locks: dict[str, threading.Lock] = {}
        self._human_handoff_pending = False

    def set_human_handoff_pending(self, pending: bool) -> None:
        self._human_handoff_pending = bool(pending)

    def _require_handoff_resolved(self) -> None:
        if self._human_handoff_pending:
            raise HumanHandoffPendingError("WAITING_FOR_HUMAN_HANDOFF")

    def register_adapter(self, agent_id: str, adapter: AgentAdapter, generation: int | None = None) -> None:
        self._adapters[agent_id] = adapter
        if generation is not None:
            self._adapter_generations[agent_id] = generation

    def create_task(self, title: str, instruction: str, *, task_id: str | None = None, parent_task_id: str | None = None) -> TaskRecord:
        self.safety.require_task_advancement_allowed()
        self._require_handoff_resolved()
        from uuid import uuid4
        task = TaskRecord(task_id or str(uuid4()), self.meeting_id, None, title, instruction, parent_task_id=parent_task_id)
        self._tasks[task.task_id] = task
        if self._persist_task:
            self._persist_task(task)
        self.events.publish(DomainEvent.create("TaskCreated", self.meeting_id, "TaskEngine", {"taskId": task.task_id, "title": title}))
        return task

    def get(self, task_id: str) -> TaskRecord:
        return self._tasks[task_id]

    def all(self) -> tuple[TaskRecord, ...]:
        return tuple(self._tasks.values())

    def assign_task(self, task_id: str, agent_id: str) -> TaskRecord:
        self.safety.require_task_advancement_allowed()
        self._require_handoff_resolved()
        task = self.get(task_id)
        self.registry.get(agent_id)
        if task.status not in {TaskStatus.PENDING, TaskStatus.QUEUED}:
            raise InvalidTaskTransition(f"cannot assign {task.status.value}")
        task = replace(task, assigned_agent_id=agent_id, status=TaskStatus.QUEUED)
        self._tasks[task_id] = task
        if self._persist_task:
            self._persist_task(task)
        self.registry.update(agent_id, current_task_id=task_id)
        self.events.publish(DomainEvent.create("TaskAssigned", self.meeting_id, "TaskEngine", {"taskId": task_id, "agentId": agent_id}))
        return task

    def _transition(self, task: TaskRecord, status: TaskStatus, **fields) -> TaskRecord:
        # BLOCKED records an explicit refusal to dispatch; it does not advance
        # work and remains observable while the safety gate is closed.
        if status != TaskStatus.BLOCKED:
            self.safety.require_task_advancement_allowed()
        if status not in self._TRANSITIONS[task.status]:
            raise InvalidTaskTransition(f"{task.status.value} -> {status.value} is not allowed")
        task = replace(task, status=status, **fields)
        self._tasks[task.task_id] = task
        if self._persist_task:
            self._persist_task(task)
        event = {
            TaskStatus.WORKING: "TaskStarted",
            TaskStatus.COMPLETED: "TaskCompleted",
            TaskStatus.FAILED: "TaskFailed",
        }.get(status, "TaskStatusChanged")
        self.events.publish(DomainEvent.create(event, self.meeting_id, "TaskEngine", {"taskId": task.task_id, "status": status.value}))
        return task

    def dispatch_task(self, task_id: str) -> TaskRecord:
        task = self.get(task_id)
        if task.assigned_agent_id is None:
            raise ValueError("task must be assigned before dispatch")
        agent_id = task.assigned_agent_id
        with self._dispatch_lock_guard:
            agent_lock = self._agent_dispatch_locks.setdefault(agent_id, threading.Lock())
        with agent_lock:
            task = self.get(task_id)
            if task.assigned_agent_id != agent_id:
                raise DispatchBlockedError("task assignment changed while waiting for Agent dispatch")
            try:
                self.safety.require_task_advancement_allowed()
            except DispatchBlockedError as exc:
                if task.status in {TaskStatus.PENDING, TaskStatus.QUEUED}:
                    self._transition(task, TaskStatus.BLOCKED, error=str(exc))
                raise
            self._require_handoff_resolved()

            if task.status not in {TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.BLOCKED}:
                raise InvalidTaskTransition(f"cannot dispatch {task.status.value} task")
            active_task = next((
                candidate for candidate in self._tasks.values()
                if candidate.task_id != task_id
                and candidate.assigned_agent_id == agent_id
                and candidate.status in {TaskStatus.DISPATCHED, TaskStatus.WORKING, TaskStatus.WAITING}
            ), None)
            if active_task is not None:
                raise DispatchBlockedError(
                    f"Agent {agent_id} already has active task {active_task.task_id}; continuation rejected"
                )

            adapter = self._adapters.get(agent_id)
            if adapter is None:
                raise ValueError(f"no adapter registered for {agent_id}")
            expected_generation = self._adapter_generations.get(agent_id)
            active_generation = self.registry.get(agent_id).runtime_generation
            if expected_generation is not None and active_generation != expected_generation:
                raise ValueError(f"stale runtime binding for {agent_id}: adapter={expected_generation}, active={active_generation}")
            task = self._transition(task, TaskStatus.DISPATCHED, started_at=utc_now(), error=None)
            try:
                adapter.send_task({"task_id": task.task_id, "input": task.instruction})
                return self._transition(task, TaskStatus.WORKING)
            except Exception as exc:
                failed = self._transition(task, TaskStatus.FAILED, completed_at=utc_now(), error=f"{type(exc).__name__}: {exc}")
                self.safety.observe_agent_failure(agent_id, f"dispatch failed: {type(exc).__name__}")
                return failed

    def complete_task(self, task_id: str, result: str) -> TaskRecord:
        task = self.get(task_id)
        return self._transition(task, TaskStatus.COMPLETED, completed_at=utc_now(), result=result)

    def accept_task(self, task_id: str) -> TaskRecord:
        self.safety.require_task_advancement_allowed()
        self._require_handoff_resolved()
        task = self.get(task_id)
        if task.status != TaskStatus.COMPLETED:
            raise InvalidTaskTransition(f"ACCEPT requires COMPLETED task, got {task.status.value}")
        if task.accepted:
            return task
        task = replace(task, accepted=True)
        self._tasks[task_id] = task
        if self._persist_task:
            self._persist_task(task)
        self.events.publish(DomainEvent.create("TaskAccepted", self.meeting_id, "TaskEngine", {"taskId": task_id, "status": task.status.value}))
        return task

    def fail_task(self, task_id: str, error: str) -> TaskRecord:
        return self._transition(self.get(task_id), TaskStatus.FAILED, completed_at=utc_now(), error=error)

    def cancel_task(self, task_id: str) -> TaskRecord:
        return self._transition(self.get(task_id), TaskStatus.CANCELLED, completed_at=utc_now())
