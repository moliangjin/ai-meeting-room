from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from ai_meeting_room.persistence.sqlite_store import SQLiteStore
from ai_meeting_room.product.app import Phase2Application


class FinderRuntimeTests(unittest.TestCase):
    @staticmethod
    def _write_executable(path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o700)

    def test_finder_like_env_codex_auth_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            codex = home / ".hermes" / "node" / "bin" / "codex"
            self._write_executable(codex, """
if [ "$1" = "--version" ]; then
  printf 'codex-cli 0.153.4\\n'
elif [ "$1" = "login" ] && [ "$2" = "status" ]; then
  printf 'Logged in using ChatGPT\\n'
else
  exit 2
fi
""")
            env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
            with patch.dict(os.environ, env, clear=True):
                app = Phase2Application(
                    SQLiteStore(home / "product-data" / "meeting.db"),
                    cao_base_url="http://127.0.0.1:19891",
                    adapter_factory=lambda **_: None,
                )
                try:
                    with patch(
                        "ai_meeting_room.integrations.cao.preflight.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("isolated health-probe fixture"),
                    ):
                        status = app.product_runtime_preflight()
                    self.assertEqual(status["checks"]["codex"]["status"], "READY")
                    self.assertEqual(status["checks"]["codex"]["path"], str(codex.resolve()))
                    self.assertEqual(status["checks"]["codexAuth"]["status"], "READY")
                finally:
                    app.close()

    def test_preflight_and_runtime_use_same_codex_binary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            codex = home / ".hermes" / "node" / "bin" / "codex"
            cao_server = home / ".local" / "bin" / "cao-server"
            tmux = home / ".local" / "bin" / "tmux"
            self._write_executable(codex, "printf 'codex-cli 0.153.4\\n'\n")
            self._write_executable(cao_server, "exit 0\n")
            self._write_executable(tmux, "printf 'tmux 3.7c\\n'\n")
            with patch.dict(os.environ, {
                "HOME": str(home),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            }, clear=True):
                app = Phase2Application(
                    SQLiteStore(home / "product-data" / "meeting.db"),
                    cao_base_url="http://127.0.0.1:19892",
                    adapter_factory=lambda **_: None,
                )
                try:
                    self.assertIs(app.cao_service_manager.tool_resolver, app.runtime_tool_resolver)
                    with patch(
                        "ai_meeting_room.integrations.cao.preflight.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("isolated health-probe fixture"),
                    ):
                        preflight = app.runtime_preflight()
                    codex_path = preflight.tools["codex"].path
                    launch_env = app.cao_service_manager._launch_environment(
                        app.product_paths.runtime / "cao",
                        app.product_paths.runtime / "tmp",
                    )
                    runtime_path = shutil.which("codex", path=launch_env["PATH"])
                    self.assertEqual(codex_path, str(codex.resolve()))
                    self.assertEqual(Path(runtime_path).resolve(), Path(codex_path).resolve())
                    self.assertEqual(preflight.tools["cao"].path, str(cao_server.resolve()))
                    self.assertEqual(preflight.tools["tmux"].path, str(tmux.resolve()))
                finally:
                    app.close()


if __name__ == "__main__":
    unittest.main()
