"""MiniMax Code AgentAdapter backed by an external Docker-compatible sandbox."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Mapping

from ai_meeting_room.agents.adapter import AgentAdapter
from ai_meeting_room.models import AgentHealth
from ai_meeting_room.runtime.errors import RuntimeErrorCode, RuntimeFailure, classify_runtime_error
from ai_meeting_room.runtime.events import RuntimeEvent, extract_final_output, parse_stream_json_line
from ai_meeting_room.tasks.envelope import TaskEnvelope

from .runtime import MiniMaxSandboxRuntime


class MiniMaxSandboxAdapter(AgentAdapter):
    def __init__(self, agent_id: str, runtime: MiniMaxSandboxRuntime, *, permission: str = "off", timeout_seconds: float = 600, max_steps: int = 50, event_sink: Callable[[RuntimeEvent], None] | None = None, failure_sink: Callable[[RuntimeFailure], None] | None = None) -> None:
        self.agent_id = agent_id
        self.runtime = runtime
        self.permission = permission
        self.timeout_seconds = timeout_seconds
        self.max_steps = max_steps
        self.event_sink = event_sink
        self.failure_sink = failure_sink
        self._started = False
        self._stopped = False
        self._failure: RuntimeFailure | None = None
        self._process = None
        self._thread: threading.Thread | None = None
        self._events: list[RuntimeEvent] = []
        self._output = ""
        self._current_task_id: str | None = None
        self._start_time: float | None = None
        self._end_time: float | None = None

    @property
    def supports_persistent_session(self) -> bool: return False
    @property
    def supports_session_resume(self) -> bool: return False

    @property
    def task_completion_observed(self) -> bool:
        process = self._process
        return bool(
            process is not None
            and process.poll() is not None
            and self._failure is None
            and self._output.strip()
        )
    @property
    def failure(self) -> RuntimeFailure | None: return self._failure
    @property
    def events(self) -> tuple[RuntimeEvent, ...]: return tuple(self._events)

    def start(self) -> str:
        self.runtime.start()
        self._started = True
        self._stopped = False
        self._failure = None
        return self.agent_id

    def send_task(self, task: Mapping[str, Any] | TaskEnvelope) -> str:
        if not self._started or self._stopped:
            raise RuntimeFailure(RuntimeErrorCode.UNKNOWN, "sandbox adapter is not started")
        if self._process is not None and self._process.poll() is None:
            raise RuntimeFailure(RuntimeErrorCode.UNKNOWN, "sandbox task already active")
        prompt = task.render_prompt() if isinstance(task, TaskEnvelope) else str(task.get("input") or task.get("prompt") or task.get("message") or "")
        task_id = task.task_id if isinstance(task, TaskEnvelope) else str(task.get("task_id") or task.get("taskId") or "")
        if not prompt:
            raise ValueError("task prompt is required")
        self._events, self._output, self._failure = [], "", None
        self._current_task_id = task_id or None
        self._start_time = time.time()
        try:
            self._process = self.runtime.exec_mcode(prompt, permission=self.permission, timeout_seconds=self.timeout_seconds, max_steps=self.max_steps)
        except RuntimeFailure as exc:
            self._fail(exc)
            raise
        except Exception as exc:
            failure = RuntimeFailure(classify_runtime_error(str(exc)), "sandbox exec could not start")
            self._fail(failure)
            raise failure from exc
        self._thread = threading.Thread(target=self._collect, args=(self._process,), name=f"minimax-sandbox-{self.agent_id}", daemon=True)
        self._thread.start()
        return task_id or self.agent_id

    def _collect(self, process) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        stderr = ""
        try:
            assert process.stdout is not None
            for line in process.stdout:
                if time.monotonic() > deadline:
                    self._fail(RuntimeFailure(RuntimeErrorCode.TIMEOUT, "sandbox MiniMax exec timeout"))
                    self.runtime.interrupt()
                    break
                try:
                    event = parse_stream_json_line(line)
                except RuntimeFailure as exc:
                    self._fail(exc)
                    self.runtime.interrupt()
                    break
                self._events.append(event)
                if self.event_sink:
                    self.event_sink(event)
                if event.event_type == "exec.completed":
                    try:
                        self._output = extract_final_output(event)
                    except RuntimeFailure as exc:
                        self._fail(exc)
            if process.poll() is None:
                try:
                    process.wait(timeout=max(0.1, deadline - time.monotonic()))
                except Exception:
                    self._fail(RuntimeFailure(RuntimeErrorCode.TIMEOUT, "sandbox MiniMax exec timeout"))
                    self.runtime.interrupt()
            code = process.wait(timeout=5)
            if process.stderr:
                stderr = process.stderr.read()[-8192:]
            if self._failure is None:
                if code != 0:
                    self._fail(RuntimeFailure(classify_runtime_error(stderr, code), f"sandbox MiniMax exec exited with code {code}", exit_code=code))
                elif not self._output:
                    self._fail(RuntimeFailure(RuntimeErrorCode.UNKNOWN, "sandbox exec ended without terminal output", exit_code=code))
        except Exception as exc:
            self._fail(RuntimeFailure(RuntimeErrorCode.CONNECTION_LOST, f"sandbox collector failed: {type(exc).__name__}"))
        finally:
            self._end_time = time.time()

    def _fail(self, failure: RuntimeFailure) -> None:
        if self._failure is None:
            self._failure = failure
            event_type = "timeout" if failure.code == RuntimeErrorCode.TIMEOUT else ("policy_violation" if failure.code == RuntimeErrorCode.SECURITY_POLICY_VIOLATION else "runtime_exit")
            self.runtime.record_audit(event_type, agent_id=self.agent_id, task_id=self._current_task_id, detail=failure.code.value)
            if self.failure_sink:
                self.failure_sink(failure)

    def stop(self) -> None:
        self._stopped = True
        self.runtime.stop()

    def interrupt(self) -> None:
        self.runtime.interrupt()

    def pause(self) -> None: self.interrupt()
    def resume(self) -> None:
        if self._failure is not None:
            raise RuntimeFailure(RuntimeErrorCode.UNKNOWN, "failed sandbox requires replacement and health checks")
        self.runtime.start()
        self._started, self._stopped = True, False

    def get_status(self) -> str:
        if self._failure: return "UNKNOWN" if self._failure.code == RuntimeErrorCode.UNKNOWN else "ERROR"
        if self._process is not None and self._process.poll() is None: return "WORKING"
        return "STOPPED" if self._stopped else "IDLE"

    def get_health(self) -> str:
        if self._failure: return AgentHealth.UNKNOWN.value if self._failure.code == RuntimeErrorCode.UNKNOWN else AgentHealth.ERROR.value
        return AgentHealth.WORKING.value if self.get_status() == "WORKING" else (AgentHealth.HEALTHY.value if self.runtime.health_check() else AgentHealth.UNKNOWN.value)

    def get_output(self) -> str: return self._output
    def health_check(self) -> bool: return self._started and not self._stopped and self._failure is None and self.runtime.health_check()
