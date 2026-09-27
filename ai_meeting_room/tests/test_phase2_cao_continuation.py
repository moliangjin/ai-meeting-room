from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from ai_meeting_room.agents.adapter import AgentAdapter
from ai_meeting_room.core.safety import DispatchBlockedError
from ai_meeting_room.core.service import MeetingCore
from ai_meeting_room.integrations.cao.adapter import CaoAgentAdapter
from ai_meeting_room.models import AgentHealth, AgentRecord, AgentRole, AgentStatus, TaskStatus
from ai_meeting_room.persistence.sqlite_store import SQLiteStore
from ai_meeting_room.product.app import Phase2Application
from phase0_poc.cao_bridge import (
    CaoHttpError,
    CaoRuntimeAgent,
    is_intermediate_progress_output,
    is_unsettled_terminal_output,
)


class CaoTurnBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = CaoRuntimeAgent("http://127.0.0.1:9889", "abcd1234", "cao-continuation-test")

    def test_first_real_turn_completion_resets_turn_boundary(self) -> None:
        self.runtime.current_task_id = "turn-1"
        self.runtime.output = "previous response"
        self.runtime.raw_status = "completed"
        self.runtime.turn_work_observed = True
        self.runtime.turn_output_changed = True

        with patch.object(self.runtime, "_request", return_value={}) as request:
            self.runtime.send_task({"task_id": "turn-2", "input": "read-only probe"})

        diagnostics = self.runtime.continuation_diagnostics()
        self.assertEqual(request.call_args.args[:2], ("POST", "/terminals/abcd1234/input"))
        self.assertEqual(diagnostics["sessionId"], "cao-continuation-test")
        self.assertEqual(diagnostics["taskId"], "turn-2")
        self.assertFalse(diagnostics["outputChanged"])
        self.assertFalse(diagnostics["workObserved"])
        self.assertFalse(diagnostics["completionObserved"])

    def test_second_turn_uses_fresh_completion_cursor(self) -> None:
        self.runtime.output = "turn one response"
        self.runtime.raw_status = "completed"
        with patch.object(self.runtime, "_request", return_value={}):
            self.runtime.send_task({"task_id": "turn-2", "input": "count files read-only"})

        self.runtime.raw_status = "idle"
        with patch.object(
            self.runtime,
            "_request",
            side_effect=[
                {"output": "turn two response"},
                {"output": "turn two response"},
            ],
        ):
            self.runtime.get_output()
            self.assertFalse(self.runtime.task_completion_observed)
            self.runtime.get_output()

        diagnostics = self.runtime.continuation_diagnostics()
        self.assertEqual(diagnostics["statusAfter"], "idle")
        self.assertTrue(diagnostics["outputChanged"])
        self.assertTrue(diagnostics["completionObserved"])

    def test_idle_without_settled_final_output_does_not_complete(self) -> None:
        self.runtime.current_task_id = "turn-transient-idle"
        self.runtime.raw_status = "idle"
        self.runtime.turn_output_changed = True
        self.runtime.output = "A response that has only been observed once."

        self.assertFalse(self.runtime.task_completion_observed)

    def test_completed_status_with_active_execution_does_not_complete(self) -> None:
        self.runtime.current_task_id = "turn-active-completed"
        self.runtime.raw_status = "completed"
        self.runtime.turn_work_observed = True
        self.runtime.turn_output_changed = True
        self.runtime.output = "The terminal is still settling."

        self.assertFalse(self.runtime.task_completion_observed)

    def test_fresh_final_output_completes_only_after_stable_boundary(self) -> None:
        self.runtime.output = "previous turn response"
        self.runtime.raw_status = "completed"
        with patch.object(self.runtime, "_request", return_value={}):
            self.runtime.send_task({"task_id": "turn-settled-final", "input": "read-only probe"})

        self.runtime.raw_status = "idle"
        with patch.object(
            self.runtime,
            "_request",
            side_effect=[
                {"output": "fresh final response"},
                {"output": "fresh final response"},
            ],
        ):
            self.runtime.get_output()
            self.assertFalse(self.runtime.task_completion_observed)
            self.runtime.get_output()

        self.assertTrue(self.runtime.task_completion_observed)

    def test_working_viewport_is_not_a_completion_boundary(self) -> None:
        self.runtime.current_task_id = "turn-working"
        self.runtime.raw_status = "idle"
        self.runtime.turn_output_changed = True
        self.runtime.output = "Please inspect the repository.\n\n◦ Working (43s • esc to interrupt)"

        self.assertTrue(is_intermediate_progress_output(self.runtime.output))
        self.assertFalse(self.runtime.task_completion_observed)

    def test_final_result_after_working_viewport_is_completable(self) -> None:
        self.runtime.current_task_id = "turn-final"
        self.runtime.raw_status = "idle"
        self.runtime.turn_output_changed = True
        self.runtime.output = "Please inspect the repository.\n\nImplemented and verified the requested change."
        self.runtime.turn_output_stable_polls = 2

        self.assertFalse(is_intermediate_progress_output(self.runtime.output))
        self.assertTrue(self.runtime.task_completion_observed)

    def test_instruction_echo_and_idle_prompt_are_not_a_completion_boundary(self) -> None:
        instruction = "Inspect the repository without changing files."
        output = instruction + "\n\n› Ask Codex to do anything\n\n  gpt-5.6-luna xhigh fast · ~/workspace"
        self.runtime.current_task_id = "turn-prompt"
        self.runtime.raw_status = "idle"
        self.runtime.turn_output_changed = True
        self.runtime.turn_input = instruction
        self.runtime.output = output

        self.assertTrue(is_unsettled_terminal_output(output, instruction=instruction))
        self.assertFalse(self.runtime.task_completion_observed)

    def test_settled_work_footer_allows_terminal_viewport_result(self) -> None:
        output = (
            "Implemented and verified the requested change.\n\n"
            "─ Worked for 1m 02s ─────────────────────────\n\n"
            "› Ask Codex to do anything\n\n"
            "  gpt-5.6-luna xhigh fast · ~/workspace"
        )
        self.runtime.current_task_id = "turn-settled"
        self.runtime.raw_status = "idle"
        self.runtime.turn_output_changed = True
        self.runtime.output = output
        self.runtime.turn_output_stable_polls = 2

        self.assertFalse(is_unsettled_terminal_output(output))
        self.assertTrue(self.runtime.task_completion_observed)

    def test_settled_final_response_with_prompt_without_footer_is_valid(self) -> None:
        output = (
            "# Formal Agent Result — COMPLETE\n\n"
            "Implemented and verified the requested change.\n\n"
            "› Ask Codex to do anything\n\n"
            "  gpt-5.6-luna xhigh fast · ~/workspace"
        )
        self.runtime.current_task_id = "turn-final-prompt"
        self.runtime.raw_status = "idle"
        self.runtime.turn_work_observed = True
        self.runtime.turn_output_changed = True
        self.runtime.output = output
        self.runtime.turn_output_stable_polls = 2

        self.assertFalse(is_unsettled_terminal_output(output, settled_lifecycle_observed=True))
        self.assertTrue(self.runtime.task_completion_observed)

    def test_working_marker_anywhere_keeps_output_unsettled(self) -> None:
        output = "◦ Working (2s • esc to interrupt)\n\nA later viewport repaint"
        self.assertTrue(is_unsettled_terminal_output(output))

    def test_prompt_fragments_and_bulleted_activity_are_not_results(self) -> None:
        outputs = (
            "› Ask Codex to do",
            "Ask Codex to",
            "gpt-5.6-luna xhigh fast",
            "• Explored repository files\n◦ Read app.py",
        )
        for output in outputs:
            with self.subTest(output=output):
                self.assertTrue(is_unsettled_terminal_output(output))

    def test_cao_adapter_exposes_runtime_completion_boundary(self) -> None:
        class Runtime:
            agent_id = "cao-agent"
            task_completion_observed = True

        adapter = CaoAgentAdapter(Runtime())
        self.assertTrue(adapter.task_completion_observed)

    def test_explored_file_viewport_with_working_marker_is_not_a_result(self) -> None:
        output = (
            "Explored repository files\n"
            "Read ai_meeting_room/product/app.py\n"
            "Read phase0_poc/cao_bridge.py\n"
            "\n◦ Working (17s • esc to interrupt)"
        )
        self.runtime.current_task_id = "turn-exploration"
        self.runtime.raw_status = "idle"
        self.runtime.turn_output_changed = True
        self.runtime.output = output

        self.assertTrue(is_unsettled_terminal_output(output))
        self.assertFalse(self.runtime.task_completion_observed)

    def test_explored_output_does_not_complete_task(self) -> None:
        output = (
            "Explored repository files\n"
            "Read ai_meeting_room/product/app.py\n"
            "Searched for task completion logic"
        )
        self.runtime.current_task_id = "turn-explored-only"
        self.runtime.raw_status = "completed"
        self.runtime.turn_output_changed = True
        self.runtime.turn_output_stable_polls = 3
        self.runtime.output = output

        self.assertTrue(is_unsettled_terminal_output(output))
        self.assertFalse(self.runtime.task_completion_observed)

    def test_continuation_does_not_read_previous_result(self) -> None:
        self.runtime.output = "turn one response"
        self.runtime.raw_status = "completed"
        with patch.object(self.runtime, "_request", return_value={}):
            self.runtime.send_task({"task_id": "turn-2", "input": "count files read-only"})

        self.runtime.raw_status = "idle"
        with patch.object(self.runtime, "_request", return_value={"output": "turn one response"}):
            self.runtime.get_output()

        diagnostics = self.runtime.continuation_diagnostics()
        self.assertFalse(diagnostics["outputChanged"])
        self.assertFalse(diagnostics["completionObserved"])

        # Even if CAO reports completed after a fresh WORKING observation, the
        # old response is never accepted when its output cursor did not move.
        self.runtime.turn_work_observed = True
        self.runtime.raw_status = "completed"
        self.assertFalse(self.runtime.task_completion_observed)

    def test_timeout_preserves_original_runtime_error(self) -> None:
        with patch.object(self.runtime, "_request", return_value={}):
            self.runtime.send_task({"task_id": "turn-auth", "input": "read-only probe"})

        self.runtime.record_task_error(CaoHttpError("CAO returned unauthorized", status_code=401))
        diagnostics = self.runtime.continuation_diagnostics()

        self.assertEqual(diagnostics["errorName"], "CaoHttpError")
        self.assertEqual(diagnostics["errorStatusCode"], 401)
        self.assertEqual(diagnostics["errorClassification"], "AUTH_ERROR")

    def test_timeout_not_misclassified_as_quota(self) -> None:
        with patch.object(self.runtime, "_request", return_value={}):
            self.runtime.send_task({"task_id": "turn-timeout", "input": "read-only probe"})

        self.runtime.raw_status = "idle"
        self.runtime.record_task_timeout()
        diagnostics = self.runtime.continuation_diagnostics()

        self.assertEqual(diagnostics["errorClassification"], "CAO_CONTINUATION_NO_RESPONSE")
        self.assertIsNone(diagnostics["errorName"])
        self.assertIsNotNone(diagnostics["timeoutAt"])

    def test_session_dead_classified_explicitly(self) -> None:
        with patch.object(self.runtime, "_request", return_value={}):
            self.runtime.send_task({"task_id": "turn-dead", "input": "read-only probe"})

        self.runtime.record_task_error(CaoHttpError("CAO terminal not found", status_code=404))

        diagnostics = self.runtime.continuation_diagnostics()
        self.assertEqual(diagnostics["errorClassification"], "CAO_SESSION_NOT_REUSABLE")


class DispatchingAdapter(AgentAdapter):
    def __init__(self, *, block_first: bool = False) -> None:
        self.agent_id = "single-flight-agent"
        self.sent: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block_first = block_first

    def start(self) -> str: return self.agent_id
    def stop(self) -> None: return None
    def pause(self) -> None: return None
    def resume(self) -> None: return None
    def interrupt(self) -> None: return None
    def send_task(self, task: Mapping[str, Any]) -> str:
        self.sent.append(str(task["task_id"]))
        if self.block_first and len(self.sent) == 1:
            self.entered.set()
            if not self.release.wait(3):
                raise TimeoutError("test adapter release timed out")
        return str(task["task_id"])
    def get_status(self) -> str: return "IDLE"
    def get_health(self) -> str: return AgentHealth.HEALTHY.value
    def get_output(self) -> str: return ""
    def health_check(self) -> bool: return True


class TaskEngineSingleFlightTests(unittest.TestCase):
    def _core(self, directory: str, adapter: DispatchingAdapter) -> MeetingCore:
        core = MeetingCore.create("single flight", SQLiteStore(Path(directory) / "state.db"), meeting_id="single-flight")
        core.add_agent(AgentRecord(
            adapter.agent_id, "Worker", "codex", AgentRole.WORKER,
            status=AgentStatus.IDLE, health=AgentHealth.HEALTHY,
        ), adapter)
        core.mark_ready()
        core.start()
        return core

    def _queued(self, core: MeetingCore, task_id: str):
        task = core.create_task(task_id, "read-only continuation", task_id=task_id)
        core.tasks.assign_task(task_id, "single-flight-agent")
        return task

    def test_single_flight_allows_next_turn_after_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = DispatchingAdapter()
            core = self._core(directory, adapter)
            self._queued(core, "turn-1")
            core.tasks.dispatch_task("turn-1")
            core.tasks.complete_task("turn-1", "first result")
            self._queued(core, "turn-2")

            core.tasks.dispatch_task("turn-2")

            self.assertEqual(adapter.sent, ["turn-1", "turn-2"])
            self.assertEqual(core.tasks.get("turn-2").status, TaskStatus.WORKING)

    def test_duplicate_concurrent_continuation_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = DispatchingAdapter(block_first=True)
            core = self._core(directory, adapter)
            self._queued(core, "turn-1")
            self._queued(core, "turn-2")
            first_errors: list[BaseException] = []

            first = threading.Thread(
                target=lambda: self._dispatch_capturing(core, "turn-1", first_errors),
                daemon=True,
            )
            first.start()
            self.assertTrue(adapter.entered.wait(2))
            try:
                with self.assertRaises(DispatchBlockedError):
                    core.tasks.dispatch_task("turn-2")
            finally:
                adapter.release.set()
                first.join(timeout=3)

            self.assertFalse(first.is_alive())
            self.assertEqual(first_errors, [])
            self.assertEqual(adapter.sent, ["turn-1"])

    @staticmethod
    def _dispatch_capturing(core: MeetingCore, task_id: str, errors: list[BaseException]) -> None:
        try:
            core.tasks.dispatch_task(task_id)
        except BaseException as exc:
            errors.append(exc)


class TaskResultCollectionTests(unittest.TestCase):
    def test_interactive_provider_prompt_opens_global_pause_before_watcher_timeout(self) -> None:
        class PromptRuntime:
            raw_status = "idle"
            task_completion_observed = False

        class PromptAdapter(DispatchingAdapter):
            def __init__(self) -> None:
                super().__init__()
                self.runtime = PromptRuntime()
                self.interrupted = threading.Event()

            def send_task(self, task: Mapping[str, Any]) -> str:
                self.sent.append(str(task["task_id"]))
                self.runtime.raw_status = "waiting_user_answer"
                return str(task["task_id"])

            def get_status(self) -> str:
                return "WAITING" if self.sent else "IDLE"

            def get_output(self) -> str:
                return "A provider-owned model choice is awaiting human input." if self.sent else ""

            def interrupt(self) -> None:
                self.interrupted.set()

        with tempfile.TemporaryDirectory() as directory:
            adapter = PromptAdapter()
            app = Phase2Application(
                SQLiteStore(Path(directory) / "product.db"),
                adapter_factory=lambda **_: adapter,
            )
            try:
                meeting_id = app.create_meeting("interactive prompt gate", directory)["meeting"]["meeting_id"]
                agent_id = app.join_provider(meeting_id, "codex")["agents"][0]["agent_id"]
                app.start_meeting(meeting_id)
                task_id = app.create_task(meeting_id, "read-only", "read only", agent_id)["tasks"][-1]["task_id"]
                app.dispatch_task(meeting_id, task_id)
                self.assertTrue(adapter.interrupted.wait(3), "interactive prompt must trigger immediate interrupt")
                deadline = time.monotonic() + 3
                state = app.snapshot(meeting_id)
                while state["meeting"]["status"] != "PAUSED" and time.monotonic() < deadline:
                    time.sleep(0.01)
                    state = app.snapshot(meeting_id)
                self.assertEqual(state["meeting"]["status"], "PAUSED")
                self.assertEqual(state["circuit"]["circuitState"], "OPEN")
                self.assertTrue(state["circuit"]["stopDispatch"])
                self.assertEqual(state["tasks"][0]["status"], "WORKING")
                self.assertIn("interactive input", state["circuit"]["triggerReason"])
                self.assertNotIn("TaskCompleted", [e["event_type"] for e in state["events"]])
            finally:
                app.close()

    def test_result_arrives_before_timeout_collected_once(self) -> None:
        class TurnRuntime:
            def __init__(self) -> None:
                self.agent_id = "single-flight-agent"
                self.session_id = "cao-live-turn"
                self.terminal_id = "abcd1234"
                self.raw_status = "idle"
                self.task_completion_observed = False

        class ImmediateAdapter(DispatchingAdapter):
            def __init__(self) -> None:
                super().__init__()
                self.runtime = TurnRuntime()
                self.output = ""

            def send_task(self, task: Mapping[str, Any]) -> str:
                self.sent.append(str(task["task_id"]))
                self.runtime.raw_status = "idle"
                self.runtime.task_completion_observed = True
                self.output = "fresh read-only result"
                return str(task["task_id"])

            def get_status(self) -> str: return "IDLE"
            def get_output(self) -> str: return self.output

        with tempfile.TemporaryDirectory() as directory:
            adapter = ImmediateAdapter()
            app = Phase2Application(
                SQLiteStore(Path(directory) / "product.db"),
                adapter_factory=lambda **_: adapter,
            )
            try:
                meeting_id = app.create_meeting("completion cursor", directory)["meeting"]["meeting_id"]
                agent_id = app.join_provider(meeting_id, "codex")["agents"][0]["agent_id"]
                app.start_meeting(meeting_id)
                task_id = app.create_task(meeting_id, "read-only", "read only", agent_id)["tasks"][-1]["task_id"]
                app.dispatch_task(meeting_id, task_id)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and app._core(meeting_id).tasks.get(task_id).status != TaskStatus.COMPLETED:
                    time.sleep(0.01)

                self.assertEqual(app._core(meeting_id).tasks.get(task_id).status, TaskStatus.COMPLETED)
                completed_events = [
                    event for event in app._core(meeting_id).events.history(meeting_id)
                    if event.event_type == "TaskCompleted"
                ]
                self.assertEqual(len(completed_events), 1)
                self.assertEqual(app._core(meeting_id).tasks.get(task_id).result, "fresh read-only result")
            finally:
                app.close()

    def test_watcher_does_not_complete_instruction_echo_with_working_marker(self) -> None:
        class ViewportRuntime:
            raw_status = "idle"
            task_completion_observed = True

        class ViewportAdapter(DispatchingAdapter):
            def __init__(self) -> None:
                super().__init__()
                self.runtime = ViewportRuntime()
                self.progress_seen = threading.Event()
                self.release_result = threading.Event()

            def send_task(self, task: Mapping[str, Any]) -> str:
                self.sent.append(str(task["task_id"]))
                return str(task["task_id"])

            def get_output(self) -> str:
                if not self.sent:
                    return ""
                if not self.release_result.is_set():
                    self.progress_seen.set()
                    return "Rework instruction echo\n\n◦ Working (0s • esc to interrupt)"
                return "Final result after the provider settled."

            def get_status(self) -> str:
                return "IDLE"

        with tempfile.TemporaryDirectory() as directory:
            adapter = ViewportAdapter()
            app = Phase2Application(
                SQLiteStore(Path(directory) / "product.db"),
                adapter_factory=lambda **_: adapter,
            )
            try:
                meeting_id = app.create_meeting("completion boundary", directory)["meeting"]["meeting_id"]
                agent_id = app.join_provider(meeting_id, "codex")["agents"][0]["agent_id"]
                app.start_meeting(meeting_id)
                task_id = app.create_task(meeting_id, "read-only", "read only", agent_id)["tasks"][-1]["task_id"]
                app.dispatch_task(meeting_id, task_id)

                self.assertTrue(adapter.progress_seen.wait(2))
                # The provider is still executing even though its terminal
                # status is idle and the viewport changed.  The watcher must
                # leave the TaskEngine record WORKING until a settled result.
                self.assertEqual(app._core(meeting_id).tasks.get(task_id).status, TaskStatus.WORKING)
                self.assertFalse(any(event.event_type == "TaskCompleted" for event in app._core(meeting_id).events.history(meeting_id)))

                adapter.release_result.set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and app._core(meeting_id).tasks.get(task_id).status != TaskStatus.COMPLETED:
                    time.sleep(0.02)
                self.assertEqual(app._core(meeting_id).tasks.get(task_id).status, TaskStatus.COMPLETED)
                self.assertEqual(app._core(meeting_id).tasks.get(task_id).result, "Final result after the provider settled.")
            finally:
                adapter.release_result.set()
                app.close()

    def test_watcher_does_not_complete_changing_live_terminal_viewport(self) -> None:
        class ViewportRuntime:
            raw_status = "idle"
            task_completion_observed = True
            turn_input = "Inspect the repository without changing files."

        class ChangingViewportAdapter(DispatchingAdapter):
            def __init__(self) -> None:
                super().__init__()
                self.runtime = ViewportRuntime()
                self.viewport_seen = threading.Event()
                self.release_result = threading.Event()
                self.viewport_reads = 0

            def send_task(self, task: Mapping[str, Any]) -> str:
                self.sent.append(str(task["task_id"]))
                return str(task["task_id"])

            def get_output(self) -> str:
                if not self.sent:
                    return ""
                if not self.release_result.is_set():
                    self.viewport_reads += 1
                    self.viewport_seen.set()
                    return (
                        f"{self.runtime.turn_input}\n\n"
                        "› Ask Codex to do anything\n\n"
                        f"  gpt-5.6-luna xhigh fast · viewport-{self.viewport_reads}"
                    )
                return "Final settled response from the provider."

            def get_status(self) -> str:
                return "IDLE"

        with tempfile.TemporaryDirectory() as directory:
            adapter = ChangingViewportAdapter()
            app = Phase2Application(
                SQLiteStore(Path(directory) / "product.db"),
                adapter_factory=lambda **_: adapter,
            )
            try:
                meeting_id = app.create_meeting("changing viewport", directory)["meeting"]["meeting_id"]
                agent_id = app.join_provider(meeting_id, "codex")["agents"][0]["agent_id"]
                app.start_meeting(meeting_id)
                task_id = app.create_task(meeting_id, "read-only", adapter.runtime.turn_input, agent_id)["tasks"][-1]["task_id"]
                app.dispatch_task(meeting_id, task_id)

                self.assertTrue(adapter.viewport_seen.wait(2))
                time.sleep(1.1)
                self.assertGreaterEqual(adapter.viewport_reads, 2)
                self.assertEqual(app._core(meeting_id).tasks.get(task_id).status, TaskStatus.WORKING)
                self.assertFalse(any(event.event_type == "TaskCompleted" for event in app._core(meeting_id).events.history(meeting_id)))

                adapter.release_result.set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and app._core(meeting_id).tasks.get(task_id).status != TaskStatus.COMPLETED:
                    time.sleep(0.02)
                self.assertEqual(app._core(meeting_id).tasks.get(task_id).status, TaskStatus.COMPLETED)
                self.assertEqual(app._core(meeting_id).tasks.get(task_id).result, "Final settled response from the provider.")
            finally:
                adapter.release_result.set()
                app.close()

    def test_formal_meeting_core_to_cao_path_rejects_live_viewport(self) -> None:
        """Exercise the production adapter/coordinator boundary, not a raw fixture."""
        runtime = CaoRuntimeAgent("http://127.0.0.1:9889", "abcd1234", "cao-formal-boundary")
        adapter = CaoAgentAdapter(runtime)
        sent = threading.Event()
        release_result = threading.Event()
        progress_seen = threading.Event()

        def request(method: str, path: str, query=None, body=None):
            del body
            if path.endswith("/input"):
                sent.set()
                return {}
            if path.endswith("/output"):
                if not sent.is_set():
                    return {"output": ""}
                if not release_result.is_set():
                    progress_seen.set()
                    return {"output": "Inspect the repository.\n\n◦ Working (1s • esc to interrupt)"}
                return {"output": "Formal settled result through CAO."}
            if path.startswith("/terminals/"):
                return {"provider": "codex", "status": "idle"}
            raise AssertionError(f"unexpected CAO request: {method} {path} {query}")

        with tempfile.TemporaryDirectory() as directory, patch.object(runtime, "_request", side_effect=request):
            app = Phase2Application(
                SQLiteStore(Path(directory) / "product.db"),
                adapter_factory=lambda **_: adapter,
                monitor_poll_interval_seconds=0.05,
            )
            try:
                meeting_id = app.create_meeting("formal CAO boundary", directory)["meeting"]["meeting_id"]
                agent_id = app.join_provider(meeting_id, "codex")["agents"][0]["agent_id"]
                app.start_meeting(meeting_id)
                task_id = app.create_task(meeting_id, "formal boundary", "Inspect the repository.", agent_id)["tasks"][-1]["task_id"]
                app.dispatch_task(meeting_id, task_id)

                self.assertTrue(progress_seen.wait(2))
                self.assertEqual(app._core(meeting_id).tasks.get(task_id).status, TaskStatus.WORKING)
                self.assertFalse(any(event.event_type == "TaskCompleted" for event in app._core(meeting_id).events.history(meeting_id)))

                release_result.set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and app._core(meeting_id).tasks.get(task_id).status != TaskStatus.COMPLETED:
                    time.sleep(0.02)
                self.assertEqual(app._core(meeting_id).tasks.get(task_id).status, TaskStatus.COMPLETED)
                self.assertEqual(app._core(meeting_id).tasks.get(task_id).result, "Formal settled result through CAO.")
            finally:
                release_result.set()
                app.close()


if __name__ == "__main__":
    unittest.main()
