"""Fresh-process MiniMax Code runtime for Phase 1.2.

V1 deliberately does not use MiniMax sessions, ``--continue`` or ACP replay.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from ai_meeting_room.agents.adapter import AgentAdapter
from ai_meeting_room.models import AgentHealth
from ai_meeting_room.runtime.errors import RuntimeErrorCode, RuntimeFailure, classify_runtime_error
from ai_meeting_room.runtime.events import RuntimeEvent, extract_final_output, parse_stream_json_line
from ai_meeting_room.runtime.security import EnvironmentSanitizer, SideEffectPolicy
from ai_meeting_room.tasks.envelope import TaskEnvelope


class MiniMaxExecAdapter(AgentAdapter):
    def __init__(self, agent_id: str, workspace_path: str | Path, *, mcode_executable: str = "mcode", node_executable: str = "node", permission: str = "smart", timeout_seconds: float = 600, max_steps: int = 50, sanitizer: EnvironmentSanitizer | None = None, side_effect_policy: SideEffectPolicy | None = None, event_sink: Callable[[RuntimeEvent], None] | None = None) -> None:
        if permission not in {"smart", "full", "off"}:
            raise ValueError("permission must be smart, full, or off")
        self.agent_id = agent_id
        self.workspace_path = str(Path(workspace_path).resolve())
        self.mcode_executable = mcode_executable
        self.node_executable = node_executable
        self.permission = permission
        self.timeout_seconds = timeout_seconds
        self.max_steps = max_steps
        self.sanitizer = sanitizer or EnvironmentSanitizer()
        self.side_effect_policy = side_effect_policy or SideEffectPolicy()
        self.event_sink = event_sink
        self._started = False
        self._stopped = False
        self._intentional_stop = False
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._events: list[RuntimeEvent] = []
        self._output = ""
        self._failure: RuntimeFailure | None = None
        self._stderr = ""
        self._started_at: float | None = None
        self._ended_at: float | None = None

    @property
    def supports_persistent_session(self) -> bool:
        return False

    @property
    def supports_session_resume(self) -> bool:
        return False

    @property
    def runtime_owner(self) -> str:
        return "minimax-exec"

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
    def process_id(self) -> int | None:
        return self._process.pid if self._process and self._process.poll() is None else None

    @property
    def failure(self) -> RuntimeFailure | None:
        return self._failure

    @property
    def events(self) -> tuple[RuntimeEvent, ...]:
        return tuple(self._events)

    @property
    def started_at(self) -> float | None:
        return self._started_at

    @property
    def ended_at(self) -> float | None:
        return self._ended_at

    def start(self) -> str:
        self._started = True
        self._stopped = False
        self._intentional_stop = False
        self._failure = None
        return self.agent_id

    def send_task(self, task: Mapping[str, Any] | TaskEnvelope) -> str:
        with self._lock:
            if not self._started or self._stopped:
                raise RuntimeFailure(RuntimeErrorCode.UNKNOWN, "MiniMax exec adapter is not started")
            if self._process is not None and self._process.poll() is None:
                raise RuntimeFailure(RuntimeErrorCode.UNKNOWN, "MiniMax exec task already active")
            prompt = task.render_prompt() if isinstance(task, TaskEnvelope) else str(task.get("input") or task.get("prompt") or task.get("message") or "")
            task_id = task.task_id if isinstance(task, TaskEnvelope) else str(task.get("task_id") or task.get("taskId") or "")
            if not prompt:
                raise ValueError("task prompt is required")
            command = [self.node_executable, self.mcode_executable, "exec", "--cwd", self.workspace_path, "--permission", self.permission, "--output-format", "stream-json", "--timeout", self._duration(self.timeout_seconds), "--max-steps", str(self.max_steps), prompt]
            env = self.sanitizer.build_env(cwd=self.workspace_path)
            self._events = []
            self._output = ""
            self._stderr = ""
            self._failure = None
            self._intentional_stop = False
            self._started_at = time.time()
            try:
                self._process = subprocess.Popen(command, cwd=self.workspace_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)
            except OSError as exc:
                self._failure = RuntimeFailure(classify_runtime_error(str(exc)), "could not start MiniMax exec")
                raise self._failure from exc
            self._thread = threading.Thread(target=self._collect, args=(self._process,), name=f"minimax-exec-{self.agent_id}", daemon=True)
            self._thread.start()
            return task_id or self.agent_id

    def _collect(self, process: subprocess.Popen[str]) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        stderr_holder: list[str] = []

        def read_stderr() -> None:
            if process.stderr:
                stderr_holder.append(process.stderr.read()[-8192:])

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                if time.monotonic() > deadline and process.poll() is None:
                    self._set_failure(RuntimeErrorCode.TIMEOUT, "MiniMax exec timeout")
                    self._terminate_process(process)
                    break
                try:
                    event = parse_stream_json_line(line)
                except RuntimeFailure as exc:
                    self._set_failure(exc.code, str(exc))
                    self._terminate_process(process)
                    break
                self._events.append(event)
                if self.event_sink:
                    self.event_sink(event)
                if event.event_type == "exec.completed":
                    try:
                        self._output = extract_final_output(event)
                    except RuntimeFailure as exc:
                        self._set_failure(exc.code, str(exc))
            if process.poll() is None:
                try:
                    process.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    self._set_failure(RuntimeErrorCode.TIMEOUT, "MiniMax exec timeout")
                    self._terminate_process(process)
            return_code = process.wait(timeout=5)
            self._stderr = "".join(stderr_holder)[-8192:]
            if self._failure is None:
                if return_code != 0:
                    code = classify_runtime_error(self._stderr, return_code)
                    self._set_failure(code, f"MiniMax exec exited with code {return_code}", exit_code=return_code)
                elif not self._output:
                    self._set_failure(RuntimeErrorCode.UNKNOWN, "MiniMax exec ended without a terminal output", exit_code=return_code)
        except Exception as exc:
            self._set_failure(RuntimeErrorCode.CONNECTION_LOST, f"MiniMax exec collector failed: {type(exc).__name__}")
        finally:
            self._ended_at = time.time()

    def _set_failure(self, code: RuntimeErrorCode, message: str, *, exit_code: int | None = None) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = RuntimeFailure(code, message, exit_code=exit_code)

    @staticmethod
    def _duration(seconds: float) -> str:
        return f"{max(1, int(seconds))}s"

    def _terminate_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGINT)
            process.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def interrupt(self) -> None:
        self._intentional_stop = True
        process = self._process
        if process and process.poll() is None:
            self._terminate_process(process)

    def stop(self) -> None:
        self._intentional_stop = True
        self._stopped = True
        self.interrupt()

    def pause(self) -> None:
        self.interrupt()

    def resume(self) -> None:
        if self._failure is not None:
            raise RuntimeFailure(RuntimeErrorCode.UNKNOWN, "failed runtime requires replacement and health check")
        self._started = True
        self._stopped = False

    def get_status(self) -> str:
        if self._failure:
            return "UNKNOWN" if self._failure.code == RuntimeErrorCode.UNKNOWN else "ERROR"
        if self._process and self._process.poll() is None:
            return "WORKING"
        return "STOPPED" if self._stopped else "IDLE"

    def get_health(self) -> str:
        if self._failure:
            return AgentHealth.UNKNOWN.value if self._failure.code == RuntimeErrorCode.UNKNOWN else AgentHealth.ERROR.value
        return AgentHealth.WORKING.value if self.get_status() == "WORKING" else AgentHealth.HEALTHY.value

    def get_output(self) -> str:
        return self._output

    def health_check(self) -> bool:
        return self._started and not self._stopped and self._failure is None
