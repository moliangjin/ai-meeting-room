from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_meeting_room.runtime.tools import RuntimeToolResolver, ToolStatus


class RuntimeToolResolverTests(unittest.TestCase):
    def test_controlled_path_includes_user_local_uv_bin_for_gui_launches(self) -> None:
        resolver = RuntimeToolResolver(environment={"PATH": "/missing"})
        self.assertIn(str(Path.home() / ".local" / "bin"), resolver.controlled_path_entries)

    def test_resolves_hermes_managed_codex_from_gui_launch_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            bin_dir = home / ".hermes" / "node" / "bin"
            bin_dir.mkdir(parents=True)
            executable = bin_dir / "codex"
            executable.write_text("#!/bin/sh\nprintf 'codex-cli 0.153.4\\n'\n", encoding="utf-8")
            executable.chmod(0o700)
            with mock.patch("pathlib.Path.home", return_value=home):
                resolver = RuntimeToolResolver(environment={"PATH": "/missing"})
                result = resolver.resolve("codex")
            self.assertEqual(result.status, ToolStatus.AVAILABLE)
            self.assertEqual(result.path, str(executable.resolve()))
            self.assertEqual(result.version, "codex-cli 0.153.4")

    def test_finder_like_env_resolves_codex_binary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            binary_dir = home / ".hermes" / "node" / "bin"
            binary_dir.mkdir(parents=True)
            executable = binary_dir / "codex"
            executable.write_text("#!/bin/sh\nprintf 'codex-cli 0.153.4\\n'\n", encoding="utf-8")
            executable.chmod(0o700)
            resolver = RuntimeToolResolver(environment={
                "HOME": str(home),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            })
            result = resolver.resolve("codex")
            self.assertEqual(result.status, ToolStatus.AVAILABLE)
            self.assertEqual(result.path, str(executable.resolve()))

    def test_explicit_tool_override_precedes_finder_path_and_is_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            explicit = home / "selected" / "codex"
            explicit.parent.mkdir()
            explicit.write_text("#!/bin/sh\nprintf 'explicit codex\\n'\n", encoding="utf-8")
            explicit.chmod(0o700)
            earlier_path = home / "path" / "codex"
            earlier_path.parent.mkdir()
            earlier_path.write_text("#!/bin/sh\nprintf 'path codex\\n'\n", encoding="utf-8")
            earlier_path.chmod(0o700)
            resolver = RuntimeToolResolver(environment={
                "HOME": str(home),
                "PATH": str(earlier_path.parent),
                "AI_MEETING_ROOM_CODEX_PATH": str(explicit),
            })
            result = resolver.resolve("codex")
            self.assertEqual(result.status, ToolStatus.AVAILABLE)
            self.assertEqual(result.path, str(explicit.resolve()))
            self.assertEqual(shutil.which("codex", path=resolver.controlled_path), str(explicit.parent / "codex"))

    def test_cao_and_tmux_resolve_without_interactive_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            bin_dir = home / ".local" / "bin"
            bin_dir.mkdir(parents=True)
            for name in ("cao-server", "tmux"):
                executable = bin_dir / name
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o700)
            resolver = RuntimeToolResolver(environment={
                "HOME": str(home),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            })
            self.assertEqual(resolver.resolve_path("cao-server").path, str((bin_dir / "cao-server").resolve()))
            self.assertEqual(resolver.resolve_path("tmux").path, str((bin_dir / "tmux").resolve()))

    def test_resolves_from_extra_path_and_returns_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "aimr-tool"
            executable.write_text("#!/bin/sh\nprintf 'aimr-tool 1.0\\n'\n", encoding="utf-8")
            executable.chmod(0o755)
            resolver = RuntimeToolResolver(environment={"PATH": "/missing", "HOME": "/tmp"}, extra_paths=(directory,))
            result = resolver.resolve("aimr-tool")
            self.assertEqual(result.status, ToolStatus.AVAILABLE)
            self.assertEqual(result.path, str(executable.resolve()))
            self.assertEqual(result.version, "aimr-tool 1.0")

    def test_controlled_environment_excludes_unapproved_keys(self) -> None:
        resolver = RuntimeToolResolver(environment={"PATH": "/bin", "HOME": "/tmp", "SECRET_TOKEN": "redacted"})
        environment = resolver.controlled_environment()
        self.assertIn("PATH", environment)
        self.assertIn("HOME", environment)
        self.assertNotIn("SECRET_TOKEN", environment)

    def test_version_probe_accepts_explicit_isolated_environment_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "aimr-tool"
            executable.write_text("#!/bin/sh\nprintf '%s\\n' \"$CAO_HOME_DIR\"\n", encoding="utf-8")
            executable.chmod(0o755)
            resolver = RuntimeToolResolver(environment={"PATH": directory, "HOME": "/Users/test", "SECRET_TOKEN": "hidden"})
            result = resolver.resolve("aimr-tool", environment_overrides={"CAO_HOME_DIR": str(Path(directory) / "cao")})
            self.assertEqual(result.status, ToolStatus.AVAILABLE)
            self.assertEqual(result.version, str(Path(directory) / "cao"))


if __name__ == "__main__":
    unittest.main()
