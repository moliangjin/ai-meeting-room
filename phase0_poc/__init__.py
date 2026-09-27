"""Phase 0 safety and adapter proof-of-concept.

This package is deliberately small. It is not the product runtime and contains
no simulated provider output.
"""

from .adapters import AgentAdapter
from .safety import (
    AgentHealth,
    AgentHeartbeat,
    CircuitState,
    GlobalCircuitBreaker,
    MeetingState,
)

__all__ = [
    "AgentAdapter",
    "AgentHealth",
    "AgentHeartbeat",
    "CircuitState",
    "GlobalCircuitBreaker",
    "MeetingState",
]
