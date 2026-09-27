"""Small synchronous EventBus used by the Phase 1 core."""

from __future__ import annotations

from collections import defaultdict
from typing import Callable

from ..models import DomainEvent


EventHandler = Callable[[DomainEvent], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._history: list[DomainEvent] = []

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)

    def publish(self, event: DomainEvent) -> None:
        self._history.append(event)
        for handler in tuple(self._handlers.get(event.event_type, ())):
            handler(event)
        for handler in tuple(self._handlers.get("*", ())):
            handler(event)

    def history(self, meeting_id: str | None = None) -> tuple[DomainEvent, ...]:
        if meeting_id is None:
            return tuple(self._history)
        return tuple(event for event in self._history if event.meeting_id == meeting_id)
