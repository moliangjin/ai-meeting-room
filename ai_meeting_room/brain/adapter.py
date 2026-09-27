"""Brain abstraction; no CLI or CAO details are allowed here."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping


class BrainAdapter(ABC):
    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def health_check(self) -> bool: ...

    @abstractmethod
    def receive_event(self, event: Mapping[str, Any]) -> None: ...

    @abstractmethod
    def decide(self) -> Mapping[str, Any] | None: ...

    @abstractmethod
    def dispatch_instruction(self, instruction: Mapping[str, Any]) -> str: ...


class DummyBrain(BrainAdapter):
    """Interface-only brain for architecture tests; never a live-AI result."""

    def __init__(self) -> None:
        self.events: list[Mapping[str, Any]] = []

    def start(self) -> None: pass
    def stop(self) -> None: pass
    def health_check(self) -> bool: return True
    def receive_event(self, event: Mapping[str, Any]) -> None: self.events.append(event)
    def decide(self) -> Mapping[str, Any] | None: return None
    def dispatch_instruction(self, instruction: Mapping[str, Any]) -> str:
        raise RuntimeError("DummyBrain cannot dispatch a real instruction")
