"""Provider-neutral Agent Adapter contract for Phase 0."""

from abc import ABC, abstractmethod
from typing import Any, Mapping


class AgentAdapter(ABC):
    """Business-layer contract; CAO/provider details stay below this seam."""

    @abstractmethod
    def start(self) -> str:
        """Start the agent and return its runtime-specific id."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the agent and release runtime resources."""

    @abstractmethod
    def pause(self) -> None:
        """Pause dispatch to the agent without assuming provider semantics."""

    @abstractmethod
    def resume(self) -> None:
        """Resume the agent after the meeting safety gate allows it."""

    @abstractmethod
    def send_task(self, task: Mapping[str, Any]) -> str:
        """Send a task and return a provider-neutral task/run id."""

    @abstractmethod
    def interrupt(self) -> None:
        """Best-effort interrupt of the active turn."""

    @abstractmethod
    def get_status(self) -> str:
        """Return a provider-neutral status string."""

    @abstractmethod
    def get_output(self) -> str:
        """Return observed output; adapters must not invent output."""

    @abstractmethod
    def health_check(self) -> bool:
        """Perform an explicit health check suitable for controlled resume."""
