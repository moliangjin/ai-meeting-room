"""Small, provider-neutral HTTP bridge for a real CAO terminal.

This is intentionally a runtime probe, not a product client.  It talks only to
CAO's loopback HTTP API and never manufactures status or output.  Provider
names are data supplied by CAO; no Codex/MiniMax branches live here.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

try:
    from .adapters import AgentAdapter
except ImportError:  # unittest discover -s phase0_poc imports this as a top-level module
    from adapters import AgentAdapter


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


# CAO 2.5.0's documented/default provider_init_timeout is 60 seconds. The
# client leaves a bounded transport margin instead of timing out first.
CAO_PROVIDER_INIT_TIMEOUT_SECONDS = 60.0
SESSION_CREATE_TRANSPORT_MARGIN_SECONDS = 15.0
DEFAULT_SESSION_CREATE_TIMEOUT_SECONDS = (
    CAO_PROVIDER_INIT_TIMEOUT_SECONDS + SESSION_CREATE_TRANSPORT_MARGIN_SECONDS
)
# CAO has no provider-neutral final-turn event.  Two identical terminal
# samples while the provider reports a settled status form the smallest
# configurable settle boundary we accept before creating a formal Result.
DEFAULT_COMPLETION_STABILITY_POLLS = 2


class SessionLifecycleState(str, Enum):
    ACTIVE = "ACTIVE"
    CREATING = "CREATING"
    READY = "READY"
    DETACHED = "DETACHED"
    FAILED = "FAILED"
    STALE = "STALE"
    CLOSED = "CLOSED"


class SessionCreateResult(str, Enum):
    NOT_CREATED = "NOT_CREATED"
    CREATING = "CREATING"
    CREATED_NOT_READY = "CREATED_NOT_READY"
    READY = "READY"
    FAILED = "FAILED"
    LATE_SUCCESS = "LATE_SUCCESS"
    UNKNOWN = "UNKNOWN"


class SessionLifecycleManager:
    """Provider-neutral lifecycle labels used during CAO reconciliation."""

    @staticmethod
    def classify_terminal_status(status: str | None) -> SessionLifecycleState:
        value = (status or "").lower()
        if value in {"idle", "completed"}:
            return SessionLifecycleState.READY
        if value in {"processing", "working", "waiting_user_answer", "waiting"}:
            return SessionLifecycleState.CREATING
        if value in {"error", "lost", "unknown"}:
            return SessionLifecycleState.FAILED
        return SessionLifecycleState.CREATING


@dataclass(frozen=True)
class SessionCreateObservation:
    request_id: str
    session_name: str
    requested_at: str
    http_result: str
    reconciled_result: str
    terminal_id: str | None
    provider_status: str

    def as_dict(self) -> dict[str, str | None]:
        return {
            "requestId": self.request_id,
            "sessionName": self.session_name,
            "requestedAt": self.requested_at,
            "httpResult": self.http_result,
            "reconciledResult": self.reconciled_result,
            "terminalId": self.terminal_id,
            "providerStatus": self.provider_status,
        }


@dataclass
class CaoAgentSnapshot:
    agent_id: str
    provider: str
    session_id: str
    terminal_id: str
    status: str
    raw_status: str
    last_seen_at: str | None
    last_output_at: str | None
    current_task_id: str | None
    output: str
    error: str | None
    start_time: str | None
    end_time: str | None


class CaoHttpError(RuntimeError):
    """A CAO transport or HTTP error; callers must treat it as a fault."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.detail = detail
        super().__init__(message)


class SessionCreateReconciler:
    """Reconcile a timed-out CAO create without issuing a second create.

    A timeout is ambiguous: the server may have committed the session while
    the client stopped waiting. This class performs only GETs plus a read-only
    ``tmux has-session`` check. It never deletes or retries a timed-out
    resource.
    """

    def __init__(
        self,
        base_url: str,
        *,
        request_timeout_seconds: float = 5.0,
        settle_seconds: float = 5.0,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        if request_timeout_seconds <= 0 or settle_seconds < 0 or poll_interval_seconds <= 0:
            raise ValueError("invalid SessionCreateReconciler timing")
        self.base_url = base_url.rstrip("/")
        self.request_timeout_seconds = request_timeout_seconds
        self.settle_seconds = settle_seconds
        self.poll_interval_seconds = poll_interval_seconds

    def _get_json(self, path: str) -> Any:
        request = urllib.request.Request(
            f"{self.base_url}{path}", headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=self.request_timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8") or "null")

    @staticmethod
    def _tmux_exists(session_name: str) -> bool:
        try:
            import shutil
            import subprocess

            tmux = shutil.which("tmux")
            if not tmux:
                return False
            return subprocess.run(
                [tmux, "has-session", "-t", session_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=2,
            ).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _inspect_once(self, session_name: str, provider: str) -> tuple[SessionCreateResult, str | None, str]:
        try:
            sessions = self._get_json("/sessions")
            if not isinstance(sessions, list):
                return SessionCreateResult.UNKNOWN, None, "INVALID_SESSION_REGISTRY"
            match = next(
                (item for item in sessions if isinstance(item, dict) and item.get("name") == session_name),
                None,
            )
            if match is None:
                return (
                    SessionCreateResult.CREATING if self._tmux_exists(session_name) else SessionCreateResult.NOT_CREATED,
                    None,
                    "TMUX_ONLY" if self._tmux_exists(session_name) else "SESSION_NOT_IN_REGISTRY",
                )
            terminals = self._get_json(
                f"/sessions/{urllib.parse.quote(session_name, safe='')}/terminals"
            )
            if not isinstance(terminals, list) or not terminals:
                return SessionCreateResult.CREATING, None, "SESSION_PRESENT_TERMINAL_PENDING"
            terminal = next(
                (item for item in terminals if isinstance(item, dict) and item.get("provider") == provider),
                terminals[0] if isinstance(terminals[0], dict) else None,
            )
            if not terminal:
                return SessionCreateResult.UNKNOWN, None, "TERMINAL_REGISTRY_INVALID"
            terminal_id = str(terminal.get("id") or "") or None
            observed_provider = str(terminal.get("provider") or "UNKNOWN")
            if observed_provider != provider:
                return SessionCreateResult.UNKNOWN, terminal_id, "PROVIDER_MISMATCH"
            state = SessionLifecycleManager.classify_terminal_status(terminal.get("status"))
            if state == SessionLifecycleState.READY:
                return SessionCreateResult.LATE_SUCCESS, terminal_id, str(terminal.get("status") or "UNKNOWN")
            if state == SessionLifecycleState.FAILED:
                return SessionCreateResult.FAILED, terminal_id, str(terminal.get("status") or "UNKNOWN")
            return SessionCreateResult.CREATED_NOT_READY, terminal_id, str(terminal.get("status") or "PROCESSING")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return SessionCreateResult.CREATING, None, "REGISTRY_NOT_READY"
            return SessionCreateResult.UNKNOWN, None, f"HTTP_{exc.code}"
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return SessionCreateResult.UNKNOWN, None, "RECONCILIATION_READ_ERROR"

    def reconcile(self, *, request_id: str, session_name: str, provider: str, requested_at: str) -> SessionCreateObservation:
        deadline = _now().timestamp() + self.settle_seconds
        last = (SessionCreateResult.UNKNOWN, None, "NO_OBSERVATION")
        while True:
            last = self._inspect_once(session_name, provider)
            if last[0] in {SessionCreateResult.LATE_SUCCESS, SessionCreateResult.FAILED, SessionCreateResult.UNKNOWN}:
                break
            if _now().timestamp() >= deadline:
                break
            import time
            time.sleep(self.poll_interval_seconds)
        result, terminal_id, provider_status = last
        return SessionCreateObservation(
            request_id=request_id,
            session_name=session_name,
            requested_at=requested_at,
            http_result="TIMEOUT",
            reconciled_result=result.value,
            terminal_id=terminal_id,
            provider_status=provider_status,
        )


def _is_quota_message(value: str) -> bool:
    """Recognize an actionable quota failure, not an informational banner.

    Codex renders an informational startup line such as ``You have 2 usage
    limit resets available``.  Treating the substring ``usage limit`` as a
    failure poisons an otherwise healthy terminal before the first task.  The
    bridge therefore requires a failure-shaped phrase (or an explicit quota /
    balance error) instead of matching generic usage-limit prose.
    """
    lowered = " ".join((value or "").lower().split())
    failure_phrases = (
        "you've hit your usage limit",
        "you have hit your usage limit",
        "you hit your usage limit",
        "usage limit has been reached",
        "usage limit reached",
        "quota exceeded",
        "quota exhausted",
        "rate limit exceeded",
        "too many requests",
        "insufficient balance",
        "insufficient funds",
    )
    return any(phrase in lowered for phrase in failure_phrases)


# CAO's last-output endpoint can expose the live terminal viewport while the
# provider is still executing.  In that interval the viewport commonly ends
# with a Codex progress line such as ``◦ Working (43s • esc to interrupt)``.
# That line is transport/UI state, never an Agent Result.  Keep this predicate
# provider-neutral and deliberately anchored to the final non-empty line so an
# instruction echo above it cannot be mistaken for a completed turn.
_WORKING_PROGRESS_LINE = re.compile(
    r"^\s*(?:[^\w\s]\s*)?working\s*\([^\r\n]*\)\s*$",
    re.IGNORECASE,
)
_TERMINAL_PROMPT_LINE = re.compile(
    r"^\s*[›>]\s*ask\s+codex\b.*$",
    re.IGNORECASE,
)
_TERMINAL_PROMPT_FRAGMENT_LINE = re.compile(
    r"^\s*ask\s+codex\b(?:\s+to\b.*)?$",
    re.IGNORECASE,
)
_TERMINAL_ATTRIBUTION_LINE = re.compile(
    r"^\s*gpt-[^\s]+(?:\s+.*)?$",
    re.IGNORECASE,
)
_SETTLED_WORK_FOOTER = re.compile(
    r"^\s*[-─—]+\s*worked\s+for\b.*$",
    re.IGNORECASE,
)
_INTERMEDIATE_TOOL_LINE = re.compile(
    r"^\s*(?:[•◦*-]\s*)?(?:(?:explored|read|searched|inspected|listed|opened|ran|running|executing|updated)(?:\s+|:).*)$",
    re.IGNORECASE,
)


def is_intermediate_progress_output(value: str) -> bool:
    """Return whether terminal output still ends at a live progress marker.

    This is intentionally a conservative completion-boundary check: a result
    may contain arbitrary prose, but a final line saying the provider is
    still ``Working`` must never be persisted as a completed Task result.
    Empty output and ordinary final responses return ``False`` so existing
    timeout and completion handling remains unchanged.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    final_line = next((line for line in reversed(value.splitlines()) if line.strip()), "")
    return bool(_WORKING_PROGRESS_LINE.fullmatch(final_line))


def is_unsettled_terminal_output(
    value: str,
    *,
    instruction: str = "",
    settled_lifecycle_observed: bool = False,
) -> bool:
    """Return whether output is a live CAO viewport rather than a Result.

    CAO's ``mode=last`` endpoint exposes rendered terminal UI, not a message
    protocol.  A changed viewport can therefore contain the current input,
    the idle prompt, or the provider attribution while the process is still
    settling.  A viewport containing only prompt/attribution UI is rejected.
    A genuine response may still be followed by that UI (and some Codex
    versions omit the ``Worked for ...`` footer), so substantive content before
    the UI is retained as the settled response only after the CAO lifecycle
    has observed a settled state.  Exact instruction echo is rejected even
    when a provider omits the UI lines.  Plain adapter results remain valid,
    preserving the existing fresh-output continuation contract for providers
    that return text rather than a terminal viewport.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    lines = [line for line in value.splitlines() if line.strip()]
    if any(_WORKING_PROGRESS_LINE.fullmatch(line) for line in lines):
        return True
    has_terminal_ui = any(
        _TERMINAL_PROMPT_LINE.fullmatch(line)
        or _TERMINAL_PROMPT_FRAGMENT_LINE.fullmatch(line)
        or _TERMINAL_ATTRIBUTION_LINE.fullmatch(line)
        for line in lines
    )
    content_lines = [
        line for line in lines
        if not _TERMINAL_PROMPT_LINE.fullmatch(line)
        and not _TERMINAL_PROMPT_FRAGMENT_LINE.fullmatch(line)
        and not _TERMINAL_ATTRIBUTION_LINE.fullmatch(line)
        and not _SETTLED_WORK_FOOTER.fullmatch(line)
    ]
    has_settled_footer = any(_SETTLED_WORK_FOOTER.fullmatch(line) for line in lines)
    if not content_lines:
        return True
    # A CAO live viewport can contain only tool/activity breadcrumbs without
    # the Working footer.  Those breadcrumbs are progress evidence, not a
    # provider result, so fail closed even after the terminal briefly reports
    # idle/completed.
    if content_lines and all(_INTERMEDIATE_TOOL_LINE.fullmatch(line) for line in content_lines):
        return True
    if has_terminal_ui and not settled_lifecycle_observed and not has_settled_footer:
        return True
    normalized_output = " ".join(content_lines if has_terminal_ui else value.split())
    normalized_instruction = " ".join(str(instruction or "").split())
    return bool(normalized_instruction and normalized_output == normalized_instruction)


_SAFE_CREATE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$")


def _validate_session_create_request(
    *,
    agent_profile: str,
    provider: str,
    session_id: str,
    working_directory: str,
) -> str:
    """Validate the fields sent to CAO before opening a provider terminal."""
    if not isinstance(agent_profile, str) or not _SAFE_CREATE_NAME.fullmatch(agent_profile):
        raise CaoHttpError(
            "CAO request validation failed: invalid agent_profile",
            error_code="CAO_REQUEST_VALIDATION_ERROR",
        )
    if not isinstance(provider, str) or not _SAFE_CREATE_NAME.fullmatch(provider):
        raise CaoHttpError(
            "CAO request validation failed: invalid provider",
            error_code="CAO_REQUEST_VALIDATION_ERROR",
        )
    if not isinstance(session_id, str) or not session_id:
        raise CaoHttpError(
            "CAO request validation failed: session_id is required",
            error_code="CAO_REQUEST_VALIDATION_ERROR",
        )
    effective_name = session_id if session_id.startswith("cao-") else f"cao-{session_id}"
    if not _SAFE_CREATE_NAME.fullmatch(effective_name):
        raise CaoHttpError(
            "CAO request validation failed: invalid session_name",
            error_code="CAO_REQUEST_VALIDATION_ERROR",
        )
    directory = Path(working_directory).expanduser()
    if not directory.is_dir():
        raise CaoHttpError(
            "CAO request validation failed: working_directory is not an existing directory",
            error_code="CAO_REQUEST_VALIDATION_ERROR",
        )
    return effective_name


class CaoRuntimeAgent(AgentAdapter):
    """Adapter for one already-created CAO terminal.

    ``terminal_id`` is CAO's API id.  The tmux window name, if needed for
    diagnostics, remains in ``raw_terminal`` and is not used by business code.
    """

    _STATUS_MAP = {
        "idle": "IDLE",
        "processing": "WORKING",
        "working": "WORKING",
        "waiting_user_answer": "WAITING",
        "waiting": "WAITING",
        "completed": "IDLE",
        "error": "ERROR",
        "lost": "LOST",
        "unknown": "UNKNOWN",
    }

    def __init__(
        self,
        base_url: str,
        terminal_id: str,
        session_id: str,
        *,
        agent_id: str | None = None,
        request_timeout_seconds: float = DEFAULT_SESSION_CREATE_TIMEOUT_SECONDS,
        request_id: str | None = None,
        create_observation: SessionCreateObservation | None = None,
        completion_stability_polls: int = DEFAULT_COMPLETION_STABILITY_POLLS,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if completion_stability_polls <= 0:
            raise ValueError("completion_stability_polls must be positive")
        self.base_url = base_url.rstrip("/")
        self.terminal_id = terminal_id
        self.session_id = session_id
        self.agent_id = agent_id or terminal_id
        self.request_timeout_seconds = request_timeout_seconds
        self.completion_stability_polls = int(completion_stability_polls)
        self.request_id = request_id
        self.create_observation = create_observation
        self.lifecycle_state = SessionLifecycleState.READY
        self.provider = "UNKNOWN"
        self.status = "UNKNOWN"
        self.raw_status = "UNKNOWN"
        self.output = ""
        self.error: str | None = None
        self.error_code: str | None = None
        self.current_task_id: str | None = None
        self.started_at = _now()
        self.finished_at: datetime | None = None
        self.last_seen_at: datetime | None = None
        self.last_output_at: datetime | None = None
        self.raw_terminal: dict[str, Any] = {}
        self.turn_number = 0
        self.turn_started_at: datetime | None = None
        self.turn_last_progress_at: datetime | None = None
        self.turn_timeout_at: datetime | None = None
        self.turn_status_before: str | None = None
        self.turn_status_history: list[str] = []
        self.turn_output_baseline = ""
        self.turn_output_sample = ""
        self.turn_output_stable_polls = 0
        # Kept only in memory to distinguish an echoed current input from a
        # fresh Agent Result.  It is never persisted or returned in
        # continuation diagnostics.
        self.turn_input = ""
        self.turn_output_changed = False
        self.turn_work_observed = False
        self.turn_error_name: str | None = None
        self.turn_error_status_code: int | None = None
        self.turn_failure_classification: str | None = None
        self._state_lock = threading.RLock()

    def _request(self, method: str, path: str, query: Mapping[str, Any] | None = None,
                 body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        query_string = urllib.parse.urlencode(
            [(key, value) for key, value in (query or {}).items() if value is not None]
        )
        url = f"{self.base_url}{path}"
        if query_string:
            url = f"{url}?{query_string}"
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout_seconds) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw_detail = ""
            try:
                raw_detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            detail = raw_detail[:500] if raw_detail else None
            try:
                decoded_detail = json.loads(raw_detail) if raw_detail else {}
                if isinstance(decoded_detail, dict) and decoded_detail.get("detail"):
                    detail = str(decoded_detail["detail"])[:500]
            except json.JSONDecodeError:
                pass
            error_code = "CAO_REQUEST_VALIDATION_ERROR" if exc.code == 400 else None
            suffix = f": {detail}" if detail else ""
            raise CaoHttpError(
                f"CAO {method} {path} failed: HTTP {exc.code}{suffix}",
                status_code=exc.code,
                error_code=error_code,
                detail=detail,
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise CaoHttpError(f"CAO {method} {path} failed: {type(exc).__name__}: {exc}") from exc
        try:
            decoded = json.loads(payload) if payload else {}
        except json.JSONDecodeError as exc:
            raise CaoHttpError(f"CAO returned non-JSON for {method} {path}") from exc
        if not isinstance(decoded, dict):
            raise CaoHttpError(f"CAO returned an unexpected payload for {method} {path}")
        return decoded

    def start(self) -> str:
        """The CAO create call is separate because this adapter binds a terminal.

        Use ``create`` when the caller needs a new Session; this method is kept
        for the common adapter contract and returns the already-created agent id.
        """
        self.refresh()
        return self.agent_id

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
        request_timeout_seconds: float = DEFAULT_SESSION_CREATE_TIMEOUT_SECONDS,
    ) -> "CaoRuntimeAgent":
        """Create one real CAO session/terminal through the HTTP API."""
        request_id = str(uuid.uuid4())
        requested_at = _iso(_now()) or ""
        effective_name = _validate_session_create_request(
            agent_profile=agent_profile,
            provider=provider,
            session_id=session_id,
            working_directory=working_directory,
        )
        query = {
            "agent_profile": agent_profile,
            "provider": provider,
            "session_name": effective_name,
            "working_directory": working_directory,
            "model": model,
            # CAO supports idempotency_key; keeping the correlation id on the
            # request also makes a later operator-approved retry safe.
            "idempotency_key": request_id,
        }
        body = {"initial_message": initial_message} if initial_message else None
        probe = cls(
            base_url,
            "PENDING",
            session_id,
            request_timeout_seconds=request_timeout_seconds,
            request_id=request_id,
        )
        try:
            probe._request("GET", f"/sessions/{urllib.parse.quote(effective_name, safe='')}")
        except CaoHttpError as exc:
            if exc.status_code != 404:
                raise
        else:
            raise CaoHttpError(
                "CAO request validation failed: session_name already exists",
                status_code=400,
                error_code="CAO_REQUEST_VALIDATION_ERROR",
                detail="session_name already exists",
            )
        try:
            terminal = probe._request("POST", "/sessions", query=query, body=body)
        except CaoHttpError as exc:
            if "timeout" not in str(exc).lower() and "timed out" not in str(exc).lower():
                raise
            observation = SessionCreateReconciler(base_url).reconcile(
                request_id=request_id,
                session_name=effective_name,
                provider=provider,
                requested_at=requested_at,
            )
            if observation.reconciled_result == SessionCreateResult.LATE_SUCCESS.value and observation.terminal_id:
                return cls(
                    base_url,
                    observation.terminal_id,
                    effective_name,
                    request_timeout_seconds=request_timeout_seconds,
                    request_id=request_id,
                    create_observation=observation,
                )
            raise CaoHttpError(
                f"CAO session create timed out; reconciliation={observation.reconciled_result}",
                error_code="SESSION_CREATE_TIMEOUT_RECONCILING",
                detail=json.dumps(observation.as_dict(), ensure_ascii=False),
            ) from exc
        terminal_id = str(terminal.get("id") or "")
        if not terminal_id:
            raise CaoHttpError("CAO session creation returned no terminal id")
        observation = SessionCreateObservation(
            request_id=request_id,
            session_name=str(terminal.get("session_name") or effective_name),
            requested_at=requested_at,
            http_result="PASS",
            reconciled_result=SessionCreateResult.READY.value,
            terminal_id=terminal_id,
            provider_status=str(terminal.get("status") or "UNKNOWN"),
        )
        return cls(
            base_url,
            terminal_id,
            str(terminal.get("session_name") or session_id),
            agent_id=terminal_id,
            request_timeout_seconds=request_timeout_seconds,
            request_id=request_id,
            create_observation=observation,
        )

    def refresh(self) -> str:
        with self._state_lock:
            terminal = self._request("GET", f"/terminals/{self.terminal_id}")
            observed_at = _now()
            self.raw_terminal = terminal
            self.provider = str(terminal.get("provider") or "UNKNOWN")
            raw_status = str(terminal.get("status") or "UNKNOWN").lower()
            self.raw_status = raw_status
            self.status = self._STATUS_MAP.get(raw_status, "UNKNOWN")
            if self.error_code == "QUOTA_EXHAUSTED":
                self.raw_status = "error"
                self.status = "ERROR"
            self.last_seen_at = observed_at
            if self.current_task_id:
                status_changed = not self.turn_status_history or self.turn_status_history[-1] != raw_status
                if status_changed:
                    self.turn_status_history.append(raw_status)
                if status_changed and raw_status in {"processing", "working"}:
                    self.turn_work_observed = True
                    self.turn_last_progress_at = observed_at
                if self.task_completion_observed:
                    self.finished_at = self.finished_at or observed_at
                if raw_status == "error" and self.turn_failure_classification is None:
                    self.turn_failure_classification = "CODEX_RUNTIME_ERROR"
                elif raw_status == "lost" and self.turn_failure_classification is None:
                    self.turn_failure_classification = "CAO_SESSION_NOT_REUSABLE"
                elif raw_status == "unknown" and self.turn_failure_classification is None:
                    self.turn_failure_classification = "UNKNOWN"
            if self.status in {"ERROR", "LOST", "UNKNOWN"}:
                self.error = f"CAO terminal status={raw_status}"
                self.finished_at = self.finished_at or observed_at
            return self.status

    def stop(self) -> None:
        with self._state_lock:
            try:
                self._request("POST", f"/terminals/{self.terminal_id}/exit")
            except CaoHttpError as exit_error:
                # Codex may already have exited back to the shell when the
                # Product Shell is closed. CAO's graceful `/exit` route can
                # then fail against the errored terminal even though no Agent
                # turn is running. Only in that settled, task-free state may
                # we ask CAO to remove this exact terminal window. Unknown or
                # active states remain fail-closed.
                try:
                    terminal = self._request("GET", f"/terminals/{self.terminal_id}")
                except CaoHttpError as status_error:
                    if status_error.status_code != 404:
                        raise exit_error from status_error
                else:
                    terminal_status = str(terminal.get("status") or "unknown").lower()
                    if terminal_status != "error" or self.current_task_id is not None:
                        raise exit_error
                    try:
                        self._request("DELETE", f"/terminals/{self.terminal_id}")
                    except CaoHttpError as delete_error:
                        if delete_error.status_code != 404:
                            raise delete_error from exit_error
            self.finished_at = _now()

    def pause(self) -> None:
        self.interrupt()

    def resume(self) -> None:
        raise RuntimeError("CAO Codex/MiniMax process resume is not supported by this bridge")

    def send_task(self, task: Mapping[str, Any]) -> str:
        message = task.get("input") or task.get("message") or task.get("prompt")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("task requires a non-empty input/message/prompt")
        task_id = str(task.get("task_id") or uuid.uuid4())
        with self._state_lock:
            self._request(
                "POST",
                f"/terminals/{self.terminal_id}/input",
                query={"message": message},
            )
            now = _now()
            self.turn_number += 1
            self.turn_started_at = now
            self.turn_last_progress_at = now
            self.turn_timeout_at = None
            self.turn_status_before = self.raw_status
            self.turn_status_history = []
            self.turn_output_baseline = self.output
            self.turn_input = message
            self.turn_output_changed = False
            self.turn_output_sample = self.output
            self.turn_output_stable_polls = 0
            self.turn_work_observed = False
            self.turn_error_name = None
            self.turn_error_status_code = None
            self.turn_failure_classification = None
            self.current_task_id = task_id
            self.started_at = now
            self.finished_at = None
            self.error = None
            return task_id

    def interrupt(self) -> None:
        with self._state_lock:
            self._request("POST", f"/terminals/{self.terminal_id}/key", query={"key": "C-c"})

    def get_status(self) -> str:
        return self.refresh()

    def get_output(self) -> str:
        with self._state_lock:
            payload = self._request(
                "GET", f"/terminals/{self.terminal_id}/output", query={"mode": "last"}
            )
            output = payload.get("output")
            if not isinstance(output, str):
                raise CaoHttpError("CAO output response did not contain text output")
            if output != self.output:
                self.output = output
                self.last_output_at = _now()
                if self.current_task_id and output.strip() and output != self.turn_output_baseline:
                    self.turn_output_changed = True
                    self.turn_last_progress_at = self.last_output_at
            if self.current_task_id and output.strip():
                if output == self.turn_output_sample:
                    self.turn_output_stable_polls += 1
                else:
                    self.turn_output_stable_polls = 1
                self.turn_output_sample = output
            elif self.current_task_id:
                self.turn_output_stable_polls = 0
            if self.task_completion_observed:
                self.finished_at = self.finished_at or self.last_output_at or _now()
            if _is_quota_message(output):
                self.error_code = "QUOTA_EXHAUSTED"
                self.error = "QUOTA_EXHAUSTED"
                self.raw_status = "error"
                self.status = "ERROR"
                self.finished_at = self.finished_at or _now()
                self.turn_failure_classification = "QUOTA_ERROR"
            return output

    @property
    def task_completion_observed(self) -> bool:
        """Whether CAO reports a settled result belonging to this input turn.

        CAO exposes ``idle``/``completed`` around both settled turns and live
        terminal viewport transitions.  A current-turn output must therefore
        be newer than the turn baseline, pass the negative intermediate guard,
        and remain identical for the configured stability polls while CAO is
        settled.  An old response or a transient viewport is fail-closed.
        """
        if (
            not self.current_task_id
            or not self.output.strip()
            or not self.turn_output_changed
            or self.turn_output_stable_polls < self.completion_stability_polls
            or self.raw_status not in {"completed", "idle"}
            or is_unsettled_terminal_output(
                self.output,
                instruction=self.turn_input,
                settled_lifecycle_observed=True,
            )
        ):
            return False
        return True

    def record_task_error(self, error: BaseException) -> None:
        """Retain non-sensitive error metadata without storing exception text."""
        self.turn_error_name = type(error).__name__
        self.turn_error_status_code = getattr(error, "status_code", None)
        error_code = getattr(error, "error_code", None)
        if error_code == "QUOTA_EXHAUSTED":
            self.turn_failure_classification = "QUOTA_ERROR"
        elif self.turn_error_status_code in {401, 403}:
            self.turn_failure_classification = "AUTH_ERROR"
        elif self.turn_error_status_code == 404:
            self.turn_failure_classification = "CAO_SESSION_NOT_REUSABLE"
        elif isinstance(error, (urllib.error.URLError, ConnectionError)):
            self.turn_failure_classification = "NETWORK_ERROR"
        elif isinstance(error, TimeoutError):
            self.turn_failure_classification = "CAO_STATUS_POLL_STALLED"
        else:
            self.turn_failure_classification = "UNKNOWN"

    def record_task_timeout(self) -> None:
        """Snapshot a timeout without changing or inventing provider failure state."""
        self.turn_timeout_at = _now()
        if self.turn_failure_classification:
            return
        if self.error_code == "QUOTA_EXHAUSTED":
            self.turn_failure_classification = "QUOTA_ERROR"
        elif self.raw_status == "completed" and not self.turn_output_changed:
            self.turn_failure_classification = "CONTINUATION_COMPLETED_BUT_RESULT_NOT_COLLECTED"
        elif self.raw_status == "idle" and not self.turn_output_changed:
            self.turn_failure_classification = "CAO_CONTINUATION_NO_RESPONSE"
        elif self.raw_status in {"processing", "working"}:
            self.turn_failure_classification = "CODEX_PROCESS_STALLED"
        elif self.raw_status in {"error", "lost", "unknown"}:
            self.turn_failure_classification = {
                "error": "CODEX_RUNTIME_ERROR",
                "lost": "CAO_SESSION_NOT_REUSABLE",
                "unknown": "UNKNOWN",
            }[self.raw_status]
        else:
            self.turn_failure_classification = "UNKNOWN"

    def continuation_diagnostics(self) -> dict[str, object]:
        """Safe per-turn metadata; never includes prompt or response contents."""
        return {
            "sessionId": self.session_id,
            "terminalId": self.terminal_id,
            "taskId": self.current_task_id,
            "turnNumber": self.turn_number,
            "startTime": _iso(self.turn_started_at),
            "lastProgressAt": _iso(self.turn_last_progress_at),
            "timeoutAt": _iso(self.turn_timeout_at),
            "statusBefore": self.turn_status_before,
            "statusDuring": self.turn_status_history[-1] if self.turn_status_history else None,
            "statusAfter": self.raw_status,
            "statusHistory": list(self.turn_status_history),
            "outputChanged": self.turn_output_changed,
            "outputStablePolls": self.turn_output_stable_polls,
            "workObserved": self.turn_work_observed,
            "completionObserved": self.task_completion_observed,
            "lastOutputAt": _iso(self.last_output_at),
            "errorName": self.turn_error_name,
            "errorStatusCode": self.turn_error_status_code,
            "errorCode": self.error_code,
            "errorClassification": self.turn_failure_classification,
            "codexProcessStatus": "UNKNOWN_NOT_EXPOSED_BY_CAO_TERMINAL_API",
            "tmuxSessionStatus": "UNKNOWN_NOT_EXPOSED_BY_CAO_TERMINAL_API",
            "caoStderrClassification": "UNKNOWN_NOT_EXPOSED_BY_CAO_TERMINAL_API",
            "caoExitCode": None,
        }

    def health_check(self) -> bool:
        return self.refresh() not in {"ERROR", "LOST", "UNKNOWN"}

    def snapshot(self) -> CaoAgentSnapshot:
        """Collect only observed CAO fields plus locally observed timestamps."""
        self.refresh()
        self.get_output()
        return CaoAgentSnapshot(
            agent_id=self.agent_id,
            provider=self.provider,
            session_id=self.session_id,
            terminal_id=self.terminal_id,
            status=self.status,
            raw_status=self.raw_status,
            last_seen_at=_iso(self.last_seen_at),
            last_output_at=_iso(self.last_output_at),
            current_task_id=self.current_task_id,
            output=self.output,
            error=self.error,
            start_time=_iso(self.started_at),
            end_time=_iso(self.finished_at),
        )
