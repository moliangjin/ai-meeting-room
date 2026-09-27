"""Provider-neutral domain models for AI Meeting Room."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MeetingStatus(str, Enum):
    CREATED = "CREATED"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    RECOVERING = "RECOVERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AgentRole(str, Enum):
    BRAIN = "BRAIN"
    WORKER = "WORKER"
    REVIEWER = "REVIEWER"


class AgentStatus(str, Enum):
    STARTING = "STARTING"
    IDLE = "IDLE"
    WORKING = "WORKING"
    WAITING = "WAITING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"
    LOST = "LOST"
    UNKNOWN = "UNKNOWN"


class AgentHealth(str, Enum):
    HEALTHY = "HEALTHY"
    IDLE = "IDLE"
    WORKING = "WORKING"
    WAITING = "WAITING"
    ERROR = "ERROR"
    LOST = "LOST"
    UNKNOWN = "UNKNOWN"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    DISPATCHED = "DISPATCHED"
    WORKING = "WORKING"
    WAITING = "WAITING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"


@dataclass
class AgentRecord:
    agent_id: str
    display_name: str
    provider: str
    role: AgentRole
    status: AgentStatus = AgentStatus.STARTING
    health: AgentHealth = AgentHealth.UNKNOWN
    session_id: str | None = None
    terminal_id: str | None = None
    workspace_path: str | None = None
    current_task_id: str | None = None
    last_heartbeat_at: str | None = None
    last_output_at: str | None = None
    runtime_id: str | None = None
    runtime_generation: int = 0
    runtime_bound_at: str | None = None
    runtime_state: str = "UNBOUND"


@dataclass(frozen=True)
class RuntimeBinding:
    """The durable identity of the runtime currently bound to an Agent."""

    agent_id: str
    runtime_id: str
    provider: str
    session_name: str | None
    session_id: str | None
    terminal_id: str | None
    generation: int
    bound_at: str
    state: str = "ACTIVE"


@dataclass
class TaskRecord:
    task_id: str
    meeting_id: str
    assigned_agent_id: str | None
    title: str
    instruction: str
    status: TaskStatus = TaskStatus.PENDING
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    completed_at: str | None = None
    result: str | None = None
    error: str | None = None
    parent_task_id: str | None = None
    accepted: bool = False


@dataclass
class MeetingRecord:
    meeting_id: str
    name: str
    status: MeetingStatus = MeetingStatus.CREATED
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    paused_at: str | None = None
    completed_at: str | None = None
    pause_reason: str | None = None
    pause_triggered_by: str | None = None
    workspace_id: str | None = None
    agent_ids: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ChangeSet:
    agent_id: str
    task_id: str
    branch: str
    commit: str | None
    diff: str
    files_changed: tuple[str, ...]


@dataclass(frozen=True)
class DomainEvent:
    event_id: str
    event_type: str
    meeting_id: str
    timestamp: str
    source: str
    payload: dict[str, Any]

    @classmethod
    def create(cls, event_type: str, meeting_id: str, source: str, payload: dict[str, Any]) -> "DomainEvent":
        return cls(str(uuid4()), event_type, meeting_id, utc_now(), source, payload)


def enum_value(value: Enum | str | None) -> str | None:
    return value.value if isinstance(value, Enum) else value


def model_dict(model: Any) -> dict[str, Any]:
    values = asdict(model)
    for key, value in list(values.items()):
        if isinstance(value, Enum):
            values[key] = value.value
        elif isinstance(value, list):
            values[key] = [item.value if isinstance(item, Enum) else item for item in value]
    return values
