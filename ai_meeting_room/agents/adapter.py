"""Formal AgentAdapter contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping


class AgentAdapter(ABC):
    @property
    def task_completion_observed(self) -> bool:
        """Whether this adapter crossed its formal final-result boundary.

        Raw ``idle``/``completed`` status is deliberately not sufficient.
        Concrete adapters with a structured process/session lifecycle must
        override this property; the default is fail-closed.
        """
        return False

    @abstractmethod
    def start(self) -> str: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def pause(self) -> None: ...

    @abstractmethod
    def resume(self) -> None: ...

    @abstractmethod
    def interrupt(self) -> None: ...

    @abstractmethod
    def send_task(self, task: Mapping[str, Any]) -> str: ...

    @abstractmethod
    def get_status(self) -> str: ...

    @abstractmethod
    def get_health(self) -> str: ...

    @abstractmethod
    def get_output(self) -> str: ...

    @abstractmethod
    def health_check(self) -> bool: ...
