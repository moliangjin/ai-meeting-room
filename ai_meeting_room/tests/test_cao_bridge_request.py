import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from phase0_poc.cao_bridge import (
    CAO_PROVIDER_INIT_TIMEOUT_SECONDS,
    DEFAULT_SESSION_CREATE_TIMEOUT_SECONDS,
    CaoHttpError,
    CaoRuntimeAgent,
    SessionCreateReconciler,
    SessionCreateResult,
    SessionLifecycleManager,
    SessionLifecycleState,
    _is_quota_message,
    _validate_session_create_request,
)


class CaoBridgeRequestTests(unittest.TestCase):
    def test_stop_removes_only_error_terminal_after_graceful_exit_failure(self) -> None:
        runtime = CaoRuntimeAgent("http://cao", "terminal-1", "session-1")
        calls = []

        def request(self, method, path, query=None, body=None):
            calls.append((method, path))
            if method == "POST" and path.endswith("/exit"):
                raise CaoHttpError("exit failed", status_code=500)
            if method == "GET":
                return {"id": "terminal-1", "status": "error"}
            if method == "DELETE":
                return {"success": True}
            raise AssertionError((method, path))

        with patch.object(CaoRuntimeAgent, "_request", request):
            runtime.stop()

        self.assertEqual(calls, [
            ("POST", "/terminals/terminal-1/exit"),
            ("GET", "/terminals/terminal-1"),
            ("DELETE", "/terminals/terminal-1"),
        ])
        self.assertIsNotNone(runtime.finished_at)

    def test_stop_does_not_delete_terminal_with_unknown_or_working_state(self) -> None:
        for status in ("processing", "unknown"):
            with self.subTest(status=status):
                runtime = CaoRuntimeAgent("http://cao", "terminal-1", "session-1")
                calls = []

                def request(self, method, path, query=None, body=None):
                    calls.append((method, path))
                    if method == "POST":
                        raise CaoHttpError("exit failed", status_code=500)
                    if method == "GET":
                        return {"id": "terminal-1", "status": status}
                    raise AssertionError((method, path))

                with patch.object(CaoRuntimeAgent, "_request", request):
                    with self.assertRaises(CaoHttpError):
                        runtime.stop()
                self.assertEqual(calls, [
                    ("POST", "/terminals/terminal-1/exit"),
                    ("GET", "/terminals/terminal-1"),
                ])

    def test_stop_does_not_delete_error_terminal_during_active_task(self) -> None:
        runtime = CaoRuntimeAgent("http://cao", "terminal-1", "session-1")
        runtime.current_task_id = "task-1"
        calls = []

        def request(self, method, path, query=None, body=None):
            calls.append((method, path))
            if method == "POST":
                raise CaoHttpError("exit failed", status_code=500)
            if method == "GET":
                return {"id": "terminal-1", "status": "error"}
            raise AssertionError((method, path))

        with patch.object(CaoRuntimeAgent, "_request", request):
            with self.assertRaises(CaoHttpError):
                runtime.stop()
        self.assertNotIn(("DELETE", "/terminals/terminal-1"), calls)

    def test_create_request_forwards_explicit_model_to_cao(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            def request(self, method, path, query=None, body=None):
                calls.append((method, path, query, body))
                if method == "GET":
                    raise CaoHttpError("not found", status_code=404)
                return {"id": "terminal-1", "session_name": "cao-model-test", "status": "idle"}

            with patch.object(CaoRuntimeAgent, "_request", request):
                CaoRuntimeAgent.create(
                    "http://cao",
                    agent_profile="developer",
                    provider="codex",
                    session_id="model-test",
                    working_directory=directory,
                    model="gpt-5.6-sol",
                )

        create_call = next(call for call in calls if call[0] == "POST")
        self.assertEqual(create_call[2]["model"], "gpt-5.6-sol")

    def test_create_request_validation_accepts_effective_cao_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                _validate_session_create_request(
                    agent_profile="developer",
                    provider="codex",
                    session_id="phase2-live-a1b2c3d4",
                    working_directory=directory,
                ),
                "cao-phase2-live-a1b2c3d4",
            )

    def test_create_request_validation_rejects_missing_workspace(self) -> None:
        with self.assertRaises(CaoHttpError) as raised:
            _validate_session_create_request(
                agent_profile="developer",
                provider="codex",
                session_id="phase2-live-a1b2c3d4",
                working_directory=str(Path(tempfile.gettempdir()) / "missing-aimr-workspace"),
            )
        self.assertEqual(raised.exception.error_code, "CAO_REQUEST_VALIDATION_ERROR")

    def test_create_request_validation_rejects_unsafe_effective_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CaoHttpError) as raised:
                _validate_session_create_request(
                    agent_profile="developer",
                    provider="codex",
                    session_id="bad.name",
                    working_directory=directory,
                )
        self.assertEqual(raised.exception.error_code, "CAO_REQUEST_VALIDATION_ERROR")

    def test_client_timeout_budget_exceeds_cao_provider_init_timeout(self) -> None:
        self.assertEqual(CAO_PROVIDER_INIT_TIMEOUT_SECONDS, 60.0)
        self.assertEqual(DEFAULT_SESSION_CREATE_TIMEOUT_SECONDS, 75.0)
        self.assertGreater(DEFAULT_SESSION_CREATE_TIMEOUT_SECONDS, CAO_PROVIDER_INIT_TIMEOUT_SECONDS)

    def test_lifecycle_maps_terminal_states(self) -> None:
        self.assertEqual(SessionLifecycleManager.classify_terminal_status("idle"), SessionLifecycleState.READY)
        self.assertEqual(SessionLifecycleManager.classify_terminal_status("processing"), SessionLifecycleState.CREATING)
        self.assertEqual(SessionLifecycleManager.classify_terminal_status("error"), SessionLifecycleState.FAILED)

    def test_informational_usage_reset_banner_is_not_quota_failure(self) -> None:
        self.assertFalse(_is_quota_message("You have 2 usage limit resets available. Run /usage to use one."))

    def test_explicit_usage_limit_failure_is_quota_failure(self) -> None:
        self.assertTrue(_is_quota_message("You've hit your usage limit."))

    def test_explicit_rate_limit_failure_is_quota_failure(self) -> None:
        self.assertTrue(_is_quota_message("Request failed: rate limit exceeded"))

    def test_reconciler_adopts_late_ready_terminal_without_creating(self) -> None:
        reconciler = SessionCreateReconciler("http://cao", settle_seconds=0)

        def get_json(path: str):
            if path == "/sessions":
                return [{"name": "cao-phase2-late", "status": "active"}]
            return [{"id": "abcdef12", "provider": "codex", "status": "idle"}]

        with patch.object(reconciler, "_get_json", side_effect=get_json), patch.object(reconciler, "_tmux_exists", return_value=True):
            observation = reconciler.reconcile(
                request_id="request-1",
                session_name="cao-phase2-late",
                provider="codex",
                requested_at="2026-09-14T00:00:00+00:00",
            )
        self.assertEqual(observation.reconciled_result, SessionCreateResult.LATE_SUCCESS.value)
        self.assertEqual(observation.terminal_id, "abcdef12")
        self.assertEqual(observation.http_result, "TIMEOUT")

    def test_reconciler_does_not_retry_when_session_is_absent(self) -> None:
        reconciler = SessionCreateReconciler("http://cao", settle_seconds=0)
        with patch.object(reconciler, "_get_json", return_value=[]), patch.object(reconciler, "_tmux_exists", return_value=False):
            observation = reconciler.reconcile(
                request_id="request-2",
                session_name="cao-phase2-absent",
                provider="codex",
                requested_at="2026-09-14T00:00:00+00:00",
            )
        self.assertEqual(observation.reconciled_result, SessionCreateResult.NOT_CREATED.value)


if __name__ == "__main__":
    unittest.main()
