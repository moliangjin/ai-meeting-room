"""CAO HTTP adapter over the Phase 0 real runtime bridge."""

from __future__ import annotations

from typing import Any, Mapping

from ...agents.adapter import AgentAdapter
from ...models import AgentHealth
from phase0_poc.cao_bridge import CaoRuntimeAgent


class CaoAgentAdapter(AgentAdapter):
    """One CAO terminal, regardless of which provider owns that terminal."""

    def __init__(self, runtime: CaoRuntimeAgent) -> None:
        self.runtime = runtime

    @classmethod
    def create(
        cls,
        base_url: str,
        *,
        agent_profile: str,
        provider: str,
        session_id: str,
        working_directory: str,
        initial_message: str | None = None,
        model: str | None = None,
    ) -> "CaoAgentAdapter":
        return cls(CaoRuntimeAgent.create(
            base_url,
            agent_profile=agent_profile,
            provider=provider,
            session_id=session_id,
            working_directory=working_directory,
            initial_message=initial_message,
            model=model,
        ))

    @property
    def agent_id(self) -> str:
        return self.runtime.agent_id

    @property
    def task_completion_observed(self) -> bool:
        """Expose CAO's settled-turn boundary through the formal adapter seam."""
        return bool(self.runtime.task_completion_observed)

    def start(self) -> str:
        return self.runtime.start()

    def stop(self) -> None:
        self.runtime.stop()

    def pause(self) -> None:
        self.runtime.pause()

    def resume(self) -> None:
        self.runtime.resume()

    def interrupt(self) -> None:
        self.runtime.interrupt()

    def send_task(self, task: Mapping[str, Any]) -> str:
        return self.runtime.send_task(task)

    def get_status(self) -> str:
        return self.runtime.get_status()

    def get_health(self) -> str:
        return AgentHealth.HEALTHY.value if self.health_check() else AgentHealth.UNKNOWN.value

    def get_output(self) -> str:
        return self.runtime.get_output()

    def health_check(self) -> bool:
        return self.runtime.health_check()
