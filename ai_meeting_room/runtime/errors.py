"""Fail-closed errors for external agent runtimes."""

from __future__ import annotations

from enum import Enum


class RuntimeErrorCode(str, Enum):
    AUTH_ERROR = "AUTH_ERROR"
    PROCESS_EXITED = "PROCESS_EXITED"
    CONNECTION_LOST = "CONNECTION_LOST"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    SECURITY_POLICY_VIOLATION = "SECURITY_POLICY_VIOLATION"
    UNKNOWN = "UNKNOWN"


class RuntimeFailure(RuntimeError):
    def __init__(self, code: RuntimeErrorCode, message: str, *, exit_code: int | None = None) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(message)


class SecurityPolicyViolation(PermissionError):
    """A sandbox boundary violation; callers must route it to GLOBAL_PAUSE."""

    code = RuntimeErrorCode.SECURITY_POLICY_VIOLATION

    def __init__(self, message: str) -> None:
        super().__init__(message)


def classify_runtime_error(text: str, exit_code: int | None = None) -> RuntimeErrorCode:
    """Classify observable process errors without retaining sensitive text."""
    lowered = (text or "").lower()
    if any(token in lowered for token in ("login required", "not authenticated", "unauthorized", "authentication", "sign in")):
        return RuntimeErrorCode.AUTH_ERROR
    if any(token in lowered for token in ("429", "rate limit", "too many requests")):
        return RuntimeErrorCode.RATE_LIMIT
    if any(token in lowered for token in ("quota", "insufficient balance", "insufficient funds", "credit")):
        return RuntimeErrorCode.QUOTA_EXHAUSTED
    return RuntimeErrorCode.PROCESS_EXITED if exit_code is not None else RuntimeErrorCode.UNKNOWN
