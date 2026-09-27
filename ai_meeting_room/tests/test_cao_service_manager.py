from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_meeting_room.product.operations import ProductDataPaths
from ai_meeting_room.runtime.cao_service_manager import CaoServiceManager, CaoServiceError


class FakeProcess:
    pid = 4242
    returncode = None


class CaoServiceManagerTests(unittest.TestCase):
    def test_default_shutil_lookup_receives_path_by_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "cao-server"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o700)

            def fail_to_spawn(*_args, **_kwargs):
                raise OSError("isolated spawn sentinel")

            manager = CaoServiceManager(
                "http://127.0.0.1:19889",
                ProductDataPaths.from_root(Path(directory) / "data"),
                environment={"PATH": directory, "HOME": "/Users/test"},
                health_probe=lambda _url: None,
                port_probe=lambda _host, _port: False,
                # Keep the real shutil.which default: this is the production
                # call signature that the packaged recovery path must use.
                popen_factory=fail_to_spawn,
            )
            with self.assertRaises(CaoServiceError) as error:
                manager.recover()
            self.assertEqual(error.exception.code, "CAO_START_FAILED")

    def test_healthy_local_service_is_reused_without_spawning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            manager = CaoServiceManager(
                "http://127.0.0.1:19889",
                ProductDataPaths.from_root(directory),
                environment={"PATH": "/usr/bin", "HOME": "/Users/test"},
                health_probe=lambda _url: {"status": "ok", "terminal_backend": "tmux"},
                port_probe=lambda _host, _port: True,
                executable_lookup=lambda _name, path: "/Users/test/.local/bin/cao-server",
                popen_factory=lambda *_args, **_kwargs: calls.append("spawn"),
            )
            result = manager.recover()
            self.assertEqual(result["status"], "READY")
            self.assertEqual(result["action"], "REUSED_HEALTHY")
            self.assertEqual(result["terminalBackend"], "tmux")
            self.assertEqual(calls, [])

    def test_unknown_process_on_port_fails_closed_without_kill_or_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            manager = CaoServiceManager(
                "http://127.0.0.1:19889",
                ProductDataPaths.from_root(directory),
                environment={"PATH": "/usr/bin", "HOME": "/Users/test"},
                health_probe=lambda _url: None,
                port_probe=lambda _host, _port: True,
                executable_lookup=lambda _name, path: "/Users/test/.local/bin/cao-server",
                popen_factory=lambda *_args, **_kwargs: calls.append("spawn"),
            )
            with self.assertRaises(CaoServiceError) as error:
                manager.recover()
            self.assertEqual(error.exception.code, "CAO_PORT_CONFLICT")
            self.assertEqual(calls, [])

    def test_healthy_but_non_tmux_service_is_not_accepted_or_reconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = CaoServiceManager(
                "http://127.0.0.1:19889",
                ProductDataPaths.from_root(directory),
                environment={"PATH": "/usr/bin", "HOME": "/Users/test"},
                health_probe=lambda _url: {"status": "ok", "terminal_backend": "herdr"},
                port_probe=lambda _host, _port: True,
                executable_lookup=lambda _name, path: None,
                popen_factory=lambda *_args, **_kwargs: None,
            )
            with self.assertRaises(CaoServiceError) as error:
                manager.recover()
            self.assertEqual(error.exception.code, "CAO_TERMINAL_BACKEND_MISMATCH")

    def test_missing_server_starts_installed_cao_with_isolated_state_and_tmux(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProductDataPaths.from_root(directory)
            executable = Path(directory) / "cao-server"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o700)
            launches = []
            health = iter([None, {"status": "ok", "terminal_backend": "tmux"}])

            def popen(args, **kwargs):
                launches.append((args, kwargs))
                return FakeProcess()

            manager = CaoServiceManager(
                "http://127.0.0.1:19889",
                paths,
                environment={
                    "PATH": "/usr/bin", "HOME": "/Users/test", "USER": "tester",
                    "OPENAI_API_KEY": "must-not-propagate", "CODEX_TOKEN": "must-not-propagate",
                },
                health_probe=lambda _url: next(health),
                port_probe=lambda _host, _port: False,
                executable_lookup=lambda _name, path: str(executable),
                popen_factory=popen,
                startup_timeout_seconds=0.01,
                poll_interval_seconds=0,
            )
            result = manager.recover()
            self.assertEqual(result["status"], "READY")
            self.assertEqual(result["action"], "STARTED_BY_PRODUCT")
            self.assertEqual(result["terminalBackend"], "tmux")
            self.assertEqual(launches[0][0], [
                str(executable), "--host", "127.0.0.1",
                "--port", "19889", "--terminal", "tmux",
            ])
            env = launches[0][1]["env"]
            self.assertEqual(env["HOME"], "/Users/test")
            self.assertEqual(env["CAO_HOME_DIR"], str(paths.runtime / "cao"))
            self.assertEqual(env["TMPDIR"], str(paths.runtime / "tmp"))
            self.assertEqual(env["TMUX_TMPDIR"], str(paths.runtime / "tmux"))
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("CODEX_TOKEN", env)
            self.assertTrue((paths.runtime / "cao").is_dir())
            self.assertTrue((paths.runtime / "tmux").is_dir())

    def test_non_loopback_service_url_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = CaoServiceManager(
                "https://example.invalid:19889", ProductDataPaths.from_root(directory),
                environment={"PATH": "/usr/bin", "HOME": "/Users/test"},
                health_probe=lambda _url: None,
                port_probe=lambda _host, _port: False,
                executable_lookup=lambda _name, path: None,
                popen_factory=lambda *_args, **_kwargs: None,
            )
            with self.assertRaises(CaoServiceError) as error:
                manager.recover()
            self.assertEqual(error.exception.code, "CAO_LOCAL_ONLY_REQUIRED")


if __name__ == "__main__":
    unittest.main()
