"""Bounded, provider-neutral task context."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class TaskEnvelope:
    meeting_id: str
    task_id: str
    agent_id: str
    role: str
    project_context: dict[str, Any] = field(default_factory=dict)
    meeting_context: dict[str, Any] = field(default_factory=dict)
    previous_relevant_results: tuple[dict[str, Any], ...] = ()
    instruction: str = ""
    workspace_path: str = "."
    constraints: tuple[str, ...] = ()
    allowed_actions: tuple[str, ...] = ()
    forbidden_actions: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()

    def render_prompt(self) -> str:
        body = {
            "meeting": {"meetingId": self.meeting_id, "agentId": self.agent_id, "role": self.role},
            "projectContext": self.project_context,
            "meetingContext": self.meeting_context,
            "previousRelevantResults": list(self.previous_relevant_results),
            "task": {"taskId": self.task_id, "instruction": self.instruction, "workspacePath": self.workspace_path},
            "constraints": list(self.constraints),
            "allowedActions": list(self.allowed_actions),
            "forbiddenActions": list(self.forbidden_actions),
            "acceptanceCriteria": list(self.acceptance_criteria),
        }
        return "AI Meeting Room task envelope (JSON):\n" + json.dumps(body, ensure_ascii=False, separators=(",", ":"))


class ContextBuilder:
    """Build only bounded context; full meeting history is never injected."""

    def build(self, meeting, task, agent, *, previous_relevant_results: Iterable[dict[str, Any]] = (), recent_events: Iterable[dict[str, Any]] = (), necessary_files: Iterable[str] = ()) -> TaskEnvelope:
        results = tuple(dict(item) for item in list(previous_relevant_results)[-5:])
        events = list(recent_events)[-8:]
        files = list(necessary_files)[:20]
        return TaskEnvelope(
            meeting_id=meeting.meeting_id,
            task_id=task.task_id,
            agent_id=agent.agent_id,
            role=getattr(agent.role, "value", str(agent.role)),
            project_context={"workspaceId": meeting.workspace_id, "necessaryFiles": files},
            meeting_context={"name": meeting.name, "status": getattr(meeting.status, "value", str(meeting.status)), "recentEvents": events},
            previous_relevant_results=results,
            instruction=task.instruction,
            workspace_path=agent.workspace_path or ".",
            constraints=("read-only unless the task explicitly authorizes a local write", "never perform external communication"),
            allowed_actions=("read_worktree", "local_validation_command"),
            forbidden_actions=("external_message", "network_write", "credential_operation", "delete_outside_workspace"),
            acceptance_criteria=("return a concise result", "report errors instead of guessing"),
        )
