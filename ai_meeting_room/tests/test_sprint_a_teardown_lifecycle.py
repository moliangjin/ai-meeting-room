from __future__ import annotations

import tempfile
import threading
import unittest
import sqlite3
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from ai_meeting_room.agents.adapter import AgentAdapter
from ai_meeting_room.models import AgentHealth
from ai_meeting_room.persistence.sqlite_store import SQLiteStore
from ai_meeting_room.product.app import Phase2Application


class ShutdownBlockingAdapter(AgentAdapter):
    def __init__(self) -> None:
        self.agent_id = "shutdown-worker"
        self.status = "IDLE"
        self.output = ""
        self.block_output_after_send = False
        self.output_entered = threading.Event()
        self.release_output = threading.Event()
        self.allow_output_return = threading.Event()
        self.stop_called = threading.Event()
        self.stop_calls = 0

    def start(self) -> str:
        return self.agent_id

    def stop(self) -> None:
        self.stop_calls += 1
        self.status = "STOPPED"
        self.stop_called.set()
        self.release_output.set()

    def pause(self) -> None:
        self.interrupt()

    def resume(self) -> None:
        self.status = "IDLE"

    def interrupt(self) -> None:
        self.status = "IDLE"

    def send_task(self, task: Mapping[str, Any]) -> str:
        self.status = "WORKING"
        self.block_output_after_send = True
        return str(task["task_id"])

    def get_status(self) -> str:
        return self.status

    def get_health(self) -> str:
        return AgentHealth.HEALTHY.value

    def get_output(self) -> str:
        if self.block_output_after_send:
            self.output_entered.set()
            self.release_output.wait()
            self.allow_output_return.wait()
            self.block_output_after_send = False
        return self.output

    def health_check(self) -> bool:
        return True


class SprintATeardownLifecycleTests(unittest.TestCase):
    def _new_running_app(self, directory: str, adapter: ShutdownBlockingAdapter, *, poll_interval: float = 30.0):
        app = Phase2Application(
            SQLiteStore(Path(directory) / "state.db"),
            adapter_factory=lambda **_: adapter,
            monitor_poll_interval_seconds=poll_interval,
        )
        meeting_id = app.create_meeting("shutdown lifecycle", directory)["meeting"]["meeting_id"]
        agent_id = app.join_provider(meeting_id, "codex")["agents"][0]["agent_id"]
        app.start_meeting(meeting_id)
        return app, meeting_id, agent_id

    def _owned_watchers(self, app: Phase2Application) -> list[threading.Thread]:
        return [
            thread
            for thread in list(app._monitor_threads.values()) + list(app._task_threads.values())
            if thread.is_alive()
        ]

    def test_heartbeat_watcher_stops_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = ShutdownBlockingAdapter()
            app, meeting_id, _ = self._new_running_app(directory, adapter)
            try:
                monitor = app._monitor_threads[meeting_id]
                app.close()

                self.assertTrue(app._monitor_stop[meeting_id].is_set())
                self.assertFalse(monitor.is_alive())
                self.assertEqual(self._owned_watchers(app), [])
            finally:
                app.close()

    def test_watcher_stop_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = ShutdownBlockingAdapter()
            app, meeting_id, _ = self._new_running_app(directory, adapter)
            try:
                app.close()
                app.close()

                self.assertEqual(adapter.stop_calls, 1)
                self.assertTrue(app._monitor_stop[meeting_id].is_set())
                self.assertEqual(self._owned_watchers(app), [])
            finally:
                app.close()

    def test_watcher_join_before_sqlite_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = ShutdownBlockingAdapter()
            app, meeting_id, agent_id = self._new_running_app(directory, adapter)
            db_path = Path(directory) / "state.db"
            task_id = app.create_task(meeting_id, "blocked output", "read-only", agent_id)["tasks"][-1]["task_id"]
            app.dispatch_task(meeting_id, task_id)
            self.assertTrue(adapter.output_entered.wait(2.0))
            task_watcher = app._task_threads[task_id]
            app.real_chrome_brain.stop = lambda **_kwargs: None
            close_errors: list[BaseException] = []

            def close_app() -> None:
                try:
                    app.close()
                except BaseException as exc:
                    close_errors.append(exc)

            closer = threading.Thread(target=close_app, name="test-sqlite-close-order")
            closer.start()
            try:
                self.assertTrue(adapter.stop_called.wait(4.0))
                self.assertTrue(closer.is_alive())
                self.assertTrue(task_watcher.is_alive())
                self.assertTrue(db_path.exists(), "temporary SQLite resource must remain until watcher join")

                adapter.allow_output_return.set()
                closer.join(2.0)
                self.assertFalse(closer.is_alive())
                self.assertFalse(task_watcher.is_alive())
                self.assertEqual(close_errors, [])
                self.assertTrue(db_path.exists())
            finally:
                adapter.release_output.set()
                adapter.allow_output_return.set()
                closer.join(2.0)
                app.close()

    def test_temp_sqlite_not_accessed_after_close(self) -> None:
        captured: list[BaseException] = []
        previous_hook = threading.excepthook
        threading.excepthook = lambda args: captured.append(args.exc_value)
        try:
            with tempfile.TemporaryDirectory() as directory:
                adapter = ShutdownBlockingAdapter()
                app, meeting_id, agent_id = self._new_running_app(directory, adapter)
                task_id = app.create_task(meeting_id, "stop before removal", "read-only", agent_id)["tasks"][-1]["task_id"]
                app.dispatch_task(meeting_id, task_id)
                self.assertTrue(adapter.output_entered.wait(2.0))
                adapter.allow_output_return.set()
                app.close()
                self.assertEqual(self._owned_watchers(app), [])
                (Path(directory) / "state.db").unlink()
        finally:
            threading.excepthook = previous_hook

        self.assertEqual(captured, [])

    def test_test_fixture_teardown_has_no_background_exception(self) -> None:
        captured: list[BaseException] = []
        previous_hook = threading.excepthook
        threading.excepthook = lambda args: captured.append(args.exc_value)
        try:
            with tempfile.TemporaryDirectory() as directory:
                adapter = ShutdownBlockingAdapter()
                app, meeting_id, agent_id = self._new_running_app(directory, adapter)
                task_id = app.create_task(meeting_id, "fixture cleanup", "read-only", agent_id)["tasks"][-1]["task_id"]
                app.dispatch_task(meeting_id, task_id)
                self.assertTrue(adapter.output_entered.wait(2.0))
                adapter.allow_output_return.set()
                try:
                    pass
                finally:
                    app.close()
                self.assertEqual(self._owned_watchers(app), [])
        finally:
            threading.excepthook = previous_hook

        self.assertEqual(captured, [])

    def test_repeated_app_start_stop_has_no_thread_leak(self) -> None:
        for _ in range(3):
            with tempfile.TemporaryDirectory() as directory:
                adapter = ShutdownBlockingAdapter()
                app, _, _ = self._new_running_app(directory, adapter)
                app.close()
                self.assertEqual(self._owned_watchers(app), [])
            active = [
                thread.name
                for thread in threading.enumerate()
                if thread is not threading.main_thread()
                and (thread.name.startswith("meeting-monitor-") or thread.name.startswith("task-watcher-"))
            ]
            self.assertEqual(active, [])

    def test_repeated_test_suite_resources_are_released(self) -> None:
        captured: list[BaseException] = []
        previous_hook = threading.excepthook
        threading.excepthook = lambda args: captured.append(args.exc_value)
        try:
            for iteration in range(3):
                with tempfile.TemporaryDirectory() as directory:
                    adapter = ShutdownBlockingAdapter()
                    app, meeting_id, agent_id = self._new_running_app(directory, adapter)
                    task_id = app.create_task(meeting_id, f"repeat-{iteration}", "read-only", agent_id)["tasks"][-1]["task_id"]
                    app.dispatch_task(meeting_id, task_id)
                    self.assertTrue(adapter.output_entered.wait(2.0))
                    adapter.allow_output_return.set()
                    app.close()
                    self.assertEqual(self._owned_watchers(app), [])
            self.assertEqual(captured, [])
        finally:
            threading.excepthook = previous_hook

    def test_store_closes_each_connection_when_operation_returns(self) -> None:
        connections: list[sqlite3.Connection] = []
        real_connect = sqlite3.connect

        def tracked_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            connection = real_connect(*args, **kwargs)
            connections.append(connection)
            return connection

        with tempfile.TemporaryDirectory() as directory:
            with patch("ai_meeting_room.persistence.sqlite_store.sqlite3.connect", side_effect=tracked_connect):
                store = SQLiteStore(Path(directory) / "state.db")
                store.save_brain_provider_config({"provider": "test"})

        operation_connection = connections[-1]
        with self.assertRaises(sqlite3.ProgrammingError):
            operation_connection.execute("SELECT 1")

    def test_application_shutdown_leaves_no_owned_watcher_threads(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        adapter = ShutdownBlockingAdapter()
        app = Phase2Application(
            SQLiteStore(Path(temporary.name) / "state.db"),
            adapter_factory=lambda **_: adapter,
        )
        # This test isolates app-owned worker teardown; the formal browser
        # Host is never connected and must not be touched from the closer thread.
        app.real_chrome_brain.stop = lambda **_kwargs: None
        closer_errors: list[BaseException] = []
        try:
            meeting_id = app.create_meeting("shutdown lifecycle", temporary.name)["meeting"]["meeting_id"]
            agent_id = app.join_provider(meeting_id, "codex")["agents"][0]["agent_id"]
            app.start_meeting(meeting_id)
            task_id = app.create_task(meeting_id, "wait for output", "read-only", agent_id)["tasks"][-1]["task_id"]
            app.dispatch_task(meeting_id, task_id)
            self.assertTrue(adapter.output_entered.wait(2.0))
            task_watcher = app._task_threads[task_id]
            monitor = app._monitor_threads[meeting_id]

            def close_app() -> None:
                try:
                    app.close()
                except BaseException as exc:  # surfaced to the test thread below
                    closer_errors.append(exc)

            close_thread = threading.Thread(target=close_app, name="test-app-closer")
            close_thread.start()
            self.assertTrue(adapter.stop_called.wait(4.0))
            self.assertTrue(close_thread.is_alive(), "close must join a task watcher before returning")

            adapter.allow_output_return.set()
            close_thread.join(2.0)
            self.assertFalse(close_thread.is_alive())
            self.assertEqual(closer_errors, [])
            self.assertFalse(task_watcher.is_alive())
            self.assertFalse(monitor.is_alive())
        finally:
            adapter.release_output.set()
            adapter.allow_output_return.set()
            app.close()
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
