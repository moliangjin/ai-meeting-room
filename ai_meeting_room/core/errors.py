"""Typed core-level lifecycle errors shared by product and CLI entry points."""

from __future__ import annotations


class MeetingLifecycleConflict(RuntimeError):
    """A lifecycle operation was requested from an incompatible state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
