from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

from ai_meeting_room import __version__
from ai_meeting_room.agents.adapter import AgentAdapter
from ai_meeting_room.data_paths import ProductDataPathError, resolve_product_paths_from_environment
from ai_meeting_room.models import AgentHealth
from ai_meeting_room.persistence.sqlite_store import SCHEMA_VERSION, SQLiteStore
from ai_meeting_room.product.app import Phase2Application
from ai_meeting_room.product.operations import (
    BackupValidationError,
    ProductDataPaths,
    V1Operations,
)
from ai_meeting_room.product.server import UI_HTML
from ai_meeting_room.product.serve import main as serve_product_main
from scripts.build_v1_app import ignored as package_ignored, validate_acceptance_config


class V1SmokeAdapter(AgentAdapter):
    def __init__(self) -> None:
        self.agent_id = "v1-smoke-agent"
        self.output = ""
        self.raw_status = "idle"

    def start(self) -> str: return self.agent_id
    def stop(self) -> None: self.raw_status = "stopped"
    def pause(self) -> None: self.raw_status = "idle"
    def resume(self) -> None: return None
    def interrupt(self) -> None: self.raw_status = "idle"
    def send_task(self, task):
        self.output = "TEST_SMOKE_RESULT: safe fixture completed"
        self.raw_status = "completed"
        return task["task_id"]
    @property
    def task_completion_observed(self) -> bool: return self.raw_status == "completed" and bool(self.output)
    def get_status(self) -> str: return "IDLE"
    def get_health(self) -> str: return AgentHealth.HEALTHY.value
    def get_output(self) -> str: return self.output
    def health_check(self) -> bool: return self.raw_status != "stopped"


class Phase3EV1AcceptanceTests(unittest.TestCase):
    def test_product_version_has_one_authoritative_file(self) -> None:
        version_file = Path(__file__).resolve().parents[1] / "VERSION"
        version = version_file.read_text(encoding="utf-8").strip()
        self.assertEqual(__version__, version)
        package_metadata = json.loads((version_file.parents[1] / "desktop" / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package_metadata["version"], version)

    def test_package_filter_keeps_runtime_source_and_excludes_user_state(self) -> None:
        excluded = package_ignored("/source/ai_meeting_room", [
            "runtime", "__pycache__", ".pytest_cache", "meeting.db", ".env.local", "chrome-brain-profile",
        ])
        self.assertNotIn("runtime", excluded)
        self.assertIn("__pycache__", excluded)
        self.assertIn(".pytest_cache", excluded)
        self.assertIn("meeting.db", excluded)
        self.assertIn(".env.local", excluded)
        self.assertIn("chrome-brain-profile", excluded)

    def test_sealed_acceptance_manifest_requires_true_mode_and_private_tmp_containment(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aimr-r69-builder-", dir="/private/tmp") as directory:
            root = Path(directory) / "product-data"
            config = {
                "schemaVersion": "ai-meeting-room.acceptance-config.v1",
                "acceptanceMode": True,
                "dataRoot": str(root),
                "productPort": 18765,
                "caoBaseUrl": "http://127.0.0.1:19889",
            }
            normalized = validate_acceptance_config(config)
            self.assertTrue(normalized["acceptanceMode"])
            self.assertEqual(Path(normalized["dataRoot"]), root.resolve())

            for invalid in (
                {**config, "acceptanceMode": False},
                {key: value for key, value in config.items() if key != "acceptanceMode"},
                {**config, "dataRoot": "/Users/test/ai-meeting-room-data"},
                {**config, "caoBaseUrl": "https://example.com"},
            ):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(ValueError):
                        validate_acceptance_config(invalid)

    def test_r70_acceptance_manifest_validates_unique_agent_session_namespace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aimr-r70-builder-", dir="/private/tmp") as directory:
            prefix = "aimr-v1-r70-a50cb28352-"
            config = {
                "schemaVersion": "ai-meeting-room.acceptance-config.v1",
                "acceptanceMode": True,
                "dataRoot": str(Path(directory) / "product-data"),
                "productPort": 62116,
                "caoBaseUrl": "http://127.0.0.1:9889",
                "agentSessionPrefix": prefix,
            }
            normalized = validate_acceptance_config(config)
            self.assertEqual(normalized["agentSessionPrefix"], prefix)
            for invalid_prefix in ("phase2-", "aimr-v1-r70-", "aimr-v1-r70-a50cb28352"):
                with self.subTest(invalid_prefix=invalid_prefix):
                    with self.assertRaises(ValueError):
                        validate_acceptance_config({**config, "agentSessionPrefix": invalid_prefix})

    def test_sealed_acceptance_manifest_rejects_symlinked_root_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aimr-r69-builder-link-", dir="/private/tmp") as directory:
            with tempfile.TemporaryDirectory(prefix="aimr-r69-builder-outside-") as outside:
                linked_root = Path(directory) / "linked-data"
                linked_root.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(ValueError):
                    validate_acceptance_config({
                        "schemaVersion": "ai-meeting-room.acceptance-config.v1",
                        "acceptanceMode": True,
                        "dataRoot": str(linked_root),
                        "productPort": 18765,
                        "caoBaseUrl": "http://127.0.0.1:19889",
                    })

    def test_data_layout_stays_outside_source_and_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProductDataPaths.from_root(Path(directory) / "product-data")
            paths.ensure()
            self.assertEqual(paths.database, paths.root / "meeting.db")
            self.assertEqual(paths.logs, paths.root / "logs")
            self.assertEqual(paths.backups, paths.root / "backups")
            self.assertEqual(paths.diagnostics, paths.root / "diagnostics")
            self.assertEqual(paths.runtime, paths.root / "runtime")
            self.assertTrue(all(path.exists() for path in (paths.logs, paths.backups, paths.diagnostics, paths.runtime)))

    def test_explicit_data_root_rejects_conflicting_database_before_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            isolated = Path(directory) / "isolated"
            outside = Path(directory) / "default-user-data" / "meeting.db"
            with self.assertRaisesRegex(ValueError, "AI_MEETING_ROOM_DB_OUTSIDE_DATA_DIR"):
                resolve_product_paths_from_environment({
                    "AI_MEETING_ROOM_DATA_DIR": str(isolated),
                    "AI_MEETING_ROOM_DB": str(outside),
                })
            self.assertFalse(isolated.exists())
            self.assertFalse(outside.parent.exists())

    def test_data_root_environment_resolves_database_and_all_runtime_paths_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "isolated"
            paths = resolve_product_paths_from_environment({"AI_MEETING_ROOM_DATA_DIR": str(root)})
            self.assertEqual(paths.root, root.resolve())
            self.assertEqual(paths.database, root.resolve() / "meeting.db")
            self.assertEqual(paths.logs, root.resolve() / "logs")
            self.assertEqual(paths.backups, root.resolve() / "backups")
            self.assertEqual(paths.runtime, root.resolve() / "runtime")

    def test_explicit_root_routes_database_inside_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "isolated"
            paths = resolve_product_paths_from_environment({"AI_MEETING_ROOM_DATA_DIR": str(root)})
            self.assertEqual(paths.database, root.resolve() / "meeting.db")

    def test_explicit_root_routes_logs_inside_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "isolated"
            paths = resolve_product_paths_from_environment({"AI_MEETING_ROOM_DATA_DIR": str(root)})
            self.assertEqual(paths.logs, root.resolve() / "logs")

    def test_explicit_root_routes_backups_inside_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "isolated"
            paths = resolve_product_paths_from_environment({"AI_MEETING_ROOM_DATA_DIR": str(root)})
            self.assertEqual(paths.backups, root.resolve() / "backups")

    def test_explicit_root_routes_runtime_metadata_inside_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "isolated"
            paths = resolve_product_paths_from_environment({"AI_MEETING_ROOM_DATA_DIR": str(root)})
            self.assertEqual(paths.runtime, root.resolve() / "runtime")

    def test_explicit_root_prevents_default_user_log_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            root = Path(directory) / "isolated"
            home.mkdir()
            with patch.dict(os.environ, {
                "HOME": str(home), "AI_MEETING_ROOM_DATA_DIR": str(root),
                "AI_MEETING_ROOM_DB": "",
                "AI_MEETING_ROOM_ACCEPTANCE_MODE": "1",
            }, clear=False):
                paths = ProductDataPaths.from_root(root)
                from ai_meeting_room.product.operations import configure_product_logging
                logger = configure_product_logging(paths)
                try:
                    logger.info("isolated-path-test")
                    self.assertTrue((root / "logs" / "product.log").is_file())
                    self.assertFalse((home / ".ai-meeting-room" / "logs" / "product.log").exists())
                finally:
                    for handler in tuple(logger.handlers):
                        logger.removeHandler(handler)
                        handler.close()

    def test_explicit_root_prevents_default_user_db_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            root = Path(directory) / "isolated"
            home.mkdir()
            with patch.dict(os.environ, {
                "HOME": str(home), "AI_MEETING_ROOM_DATA_DIR": str(root),
                "AI_MEETING_ROOM_DB": "",
                "AI_MEETING_ROOM_ACCEPTANCE_MODE": "1",
            }, clear=False):
                paths = resolve_product_paths_from_environment()
                SQLiteStore(paths.database)
                self.assertTrue(paths.database.is_file())
                self.assertFalse((home / ".ai-meeting-room" / "meeting.db").exists())

    def test_acceptance_mode_requires_explicit_root_before_logging_or_database_init(self) -> None:
        with patch.dict(os.environ, {"AI_MEETING_ROOM_ACCEPTANCE_MODE": "1"}, clear=False):
            with patch("ai_meeting_room.product.serve.configure_product_logging", side_effect=AssertionError("logging initialized before root validation")):
                with self.assertRaisesRegex(ProductDataPathError, "PRODUCT_DATA_ROOT_OVERRIDE_REQUIRED"):
                    serve_product_main([])

    def test_explicit_root_rejects_database_symlink_escape_before_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "isolated"
            root.mkdir()
            outside_database = Path(directory) / "outside.db"
            (root / "meeting.db").symlink_to(outside_database)
            with self.assertRaisesRegex(ProductDataPathError, "PRODUCT_PATH_ESCAPES_OVERRIDE_ROOT"):
                resolve_product_paths_from_environment({"AI_MEETING_ROOM_DATA_DIR": str(root)})
            self.assertFalse(outside_database.exists())

    def test_path_escape_fails_before_logging_init(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "isolated"
            external_logs = Path(directory) / "outside-logs"
            root.mkdir()
            external_logs.mkdir()
            (root / "logs").symlink_to(external_logs, target_is_directory=True)
            with patch.dict(os.environ, {"AI_MEETING_ROOM_DATA_DIR": str(root), "AI_MEETING_ROOM_DB": ""}, clear=False):
                with patch("ai_meeting_room.product.serve.configure_product_logging", side_effect=AssertionError("logging initialized before containment")):
                    with self.assertRaisesRegex(ProductDataPathError, "PRODUCT_PATH_ESCAPES_OVERRIDE_ROOT"):
                        serve_product_main([])
            self.assertFalse((external_logs / "product.log").exists())
            self.assertFalse((root / "meeting.db").exists())

    def test_path_escape_fails_before_database_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "isolated"
            root.mkdir()
            outside_database = Path(directory) / "outside-meeting.db"
            (root / "meeting.db").symlink_to(outside_database)
            with patch.dict(os.environ, {"AI_MEETING_ROOM_DATA_DIR": str(root), "AI_MEETING_ROOM_DB": ""}, clear=False):
                with patch("ai_meeting_room.product.serve.configure_product_logging", side_effect=AssertionError("logging initialized before containment")):
                    with self.assertRaisesRegex(ProductDataPathError, "PRODUCT_PATH_ESCAPES_OVERRIDE_ROOT"):
                        serve_product_main([])
            self.assertFalse(outside_database.exists())

    def test_product_server_rejects_db_escape_before_logging_or_sqlite_init(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "isolated"
            outside_database = Path(directory) / "user-data" / "meeting.db"
            with patch.dict(os.environ, {
                "AI_MEETING_ROOM_DATA_DIR": str(root),
                "AI_MEETING_ROOM_DB": str(outside_database),
            }, clear=False):
                with self.assertRaisesRegex(ValueError, "AI_MEETING_ROOM_DB_OUTSIDE_DATA_DIR"):
                    serve_product_main([])
            self.assertFalse(root.exists())
            self.assertFalse(outside_database.parent.exists())

    def test_experimental_browser_runtime_logs_follow_configured_product_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(__file__).resolve().parents[2]
            env = dict(os.environ, AI_MEETING_ROOM_DATA_DIR=directory, AI_MEETING_ROOM_DB="")
            script = (
                "import json; from ai_meeting_room.brain import playwright_attached_brain as b; "
                "print(json.dumps([str(b.CONNECT_DEBUG_LOG), str(b.CONNECT_STACK_LOG), "
                "str(b.COMPOSER_PROBE_LOG), str(b.RUNTIME_LIFECYCLE_LOG)]))"
            )
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=root, env=env,
                capture_output=True, text=True, check=True,
            )
            expected_root = Path(directory).resolve()
            self.assertEqual(
                json.loads(result.stdout),
                [str(expected_root / "logs" / name) for name in (
                    "brain-connect-debug.log", "brain-connect-stack.log",
                    "composer-probe.log", "runtime-lifecycle.log",
                )],
            )

    def test_product_shell_cli_starts_and_shuts_down_with_v1_data_paths(self) -> None:
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            env = dict(
                os.environ,
                AI_MEETING_ROOM_DATA_DIR=directory,
                AI_MEETING_ROOM_DB="",
                # This lifecycle smoke covers the Product Shell, not host CAO.
                # Keep it isolated from the canonical 9889 service and prevent
                # the new automatic startup hook from leaving a persistent CAO.
                AI_MEETING_ROOM_CAO_SERVER_PATH=str(Path(directory) / "not-installed" / "cao-server"),
            )
            process = subprocess.Popen(
                [sys.executable, "-m", "ai_meeting_room.product.serve", "--host", "127.0.0.1",
                 "--port", str(port), "--cao-url", "http://127.0.0.1:65534", "--no-legacy-extension-bridge"],
                cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                deadline = time.monotonic() + 8
                meetings: list[dict[str, object]] | None = None
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        stdout, stderr = process.communicate()
                        self.fail(f"Product Shell exited before listening: {stdout}\n{stderr}")
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/meetings", timeout=0.25) as response:
                            meetings = json.loads(response.read().decode("utf-8"))
                        break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.1)
                self.assertEqual(meetings, [])
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/runtime/shutdown",
                    data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 202)
                process.wait(timeout=8)
                stdout, stderr = process.communicate()
                self.assertEqual(process.returncode, 0, f"{stdout}\n{stderr}")
                self.assertTrue((Path(directory) / "meeting.db").is_file())
                self.assertTrue((Path(directory) / "logs" / "product.log").is_file())
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    process.wait(timeout=5)

    def test_backup_has_integrity_metadata_and_restore_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProductDataPaths.from_root(Path(directory) / "data")
            paths.ensure()
            store = SQLiteStore(paths.database)
            with sqlite3.connect(paths.database) as db:
                db.execute("CREATE TABLE IF NOT EXISTS v1_probe(value TEXT)")
                db.execute("INSERT INTO v1_probe VALUES ('before')")
                db.execute(
                    "INSERT INTO brain_provider_config(config_id,data) VALUES (1,?)",
                    (json.dumps({"provider": "openai-compatible", "secretReference": "brain-provider", "apiCredential": "v1-backup-test-secret"}),),
                )
                db.commit()
            operations = V1Operations(paths, app_version="1.0.0")
            backup = operations.create_backup()
            archive = Path(backup["path"])
            self.assertTrue(archive.is_file())
            with zipfile.ZipFile(archive) as bundle:
                self.assertEqual(set(bundle.namelist()), {"manifest.json", "meeting.db"})
                manifest = json.loads(bundle.read("manifest.json"))
                database_bytes = bundle.read("meeting.db")
            self.assertEqual(manifest["appVersion"], "1.0.0")
            self.assertEqual(manifest["schemaVersion"], SCHEMA_VERSION)
            self.assertEqual(manifest["databaseSha256"], hashlib.sha256(database_bytes).hexdigest())
            self.assertTrue(manifest["excludesCredentials"])
            self.assertNotIn(b"v1-backup-test-secret", database_bytes)
            safe_database = Path(directory) / "backup-snapshot.db"
            safe_database.write_bytes(database_bytes)
            with sqlite3.connect(paths.database) as db:
                self.assertIn("v1-backup-test-secret", db.execute(
                    "SELECT data FROM brain_provider_config WHERE config_id=1"
                ).fetchone()[0])
            with sqlite3.connect(safe_database) as db:
                saved_config = json.loads(db.execute(
                    "SELECT data FROM brain_provider_config WHERE config_id=1"
                ).fetchone()[0])
            self.assertNotIn("apiCredential", saved_config)
            self.assertEqual(saved_config["secretReference"], "brain-provider")
            self.assertEqual(operations.validate_backup(backup["backupId"])["status"], "VALID")
            with self.assertRaises(BackupValidationError) as error:
                operations.restore_backup(backup["backupId"], confirm_overwrite=False)
            self.assertEqual(error.exception.code, "RESTORE_CONFIRMATION_REQUIRED")

            with sqlite3.connect(paths.database) as db:
                db.execute("UPDATE v1_probe SET value='after'")
                db.commit()
            restored = operations.restore_backup(backup["backupId"], confirm_overwrite=True)
            self.assertTrue(restored["restartRequired"])
            with sqlite3.connect(paths.database) as db:
                self.assertEqual(db.execute("SELECT value FROM v1_probe").fetchone()[0], "before")

    def test_backup_rejects_checksum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProductDataPaths.from_root(Path(directory) / "data")
            paths.ensure()
            SQLiteStore(paths.database)
            operations = V1Operations(paths, app_version="1.0.0")
            backup = operations.create_backup()
            archive = Path(backup["path"])
            with zipfile.ZipFile(archive) as bundle:
                manifest = bundle.read("manifest.json")
            replacement = archive.with_suffix(".replacement")
            with zipfile.ZipFile(replacement, "w") as bundle:
                bundle.writestr("manifest.json", manifest)
                bundle.writestr("meeting.db", b"tampered")
            replacement.replace(archive)
            with self.assertRaises(BackupValidationError):
                operations.validate_backup(backup["backupId"])

    def test_backup_rejects_unexpected_members_and_oversized_payload_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProductDataPaths.from_root(Path(directory) / "data")
            paths.ensure()
            SQLiteStore(paths.database)
            operations = V1Operations(paths, app_version="1.0.0")
            backup = operations.create_backup()
            archive = Path(backup["path"])
            replacement = archive.with_suffix(".replacement")
            with zipfile.ZipFile(archive) as source, zipfile.ZipFile(replacement, "w") as target:
                for member in source.infolist():
                    target.writestr(member.filename, source.read(member.filename))
                target.writestr("unexpected.txt", "not allowed")
            replacement.replace(archive)
            with self.assertRaises(BackupValidationError) as error:
                operations.validate_backup(backup["backupId"])
            self.assertEqual(error.exception.code, "BACKUP_CONTENTS_INVALID")

    def test_diagnostic_export_is_bounded_and_redacts_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProductDataPaths.from_root(Path(directory) / "data")
            paths.ensure()
            SQLiteStore(paths.database)
            log = paths.logs / "product.log"
            log.write_text("startup\nAuthorization: Bearer should-not-leak\nCookie=session-secret\n", encoding="utf-8")
            (paths.logs / "desktop.log").write_text('{"event":"electron-startup"}\napi_key=desktop-secret\n', encoding="utf-8")
            operations = V1Operations(paths, app_version="1.0.0")
            result = operations.create_diagnostic_report(
                preflight={"overallStatus": "READY"},
                meetings=[{"meeting_id": "meeting-1", "status": "COMPLETED", "name": "private title"}],
                build={"commit": "abc123", "tests": "PASS"},
            )
            report = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
            encoded = json.dumps(report)
            self.assertEqual(report["appVersion"], "1.0.0")
            self.assertEqual(report["meetings"], [{"meetingId": "meeting-1", "status": "COMPLETED"}])
            self.assertNotIn("private title", encoded)
            self.assertNotIn("should-not-leak", encoded)
            self.assertNotIn("session-secret", encoded)
            self.assertNotIn("desktop-secret", encoded)
            self.assertLessEqual(len(report["recentLogs"]), 200)

    def test_schema_initialization_is_idempotent_and_versioned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            SQLiteStore(database)
            SQLiteStore(database)
            with sqlite3.connect(database) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_schema_preserves_unknown_user_tables_and_fails_closed_on_future_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE user_extension(value TEXT)")
                db.execute("INSERT INTO user_extension VALUES ('keep')")
                db.commit()
            SQLiteStore(database)
            with sqlite3.connect(database) as db:
                self.assertEqual(db.execute("SELECT value FROM user_extension").fetchone()[0], "keep")
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            with self.assertRaises(RuntimeError):
                SQLiteStore(database)

    def test_preflight_reports_data_workspace_and_port_without_optional_brain_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "state.db")
            app = Phase2Application(store, adapter_factory=lambda **_: None)
            fake = type("Preflight", (), {
                "cao_server_healthy": True,
                "as_dict": lambda self: {"tools": {
                    "cao": {"status": "AVAILABLE", "version": "2.5.0", "path": "/bin/cao"},
                    "tmux": {"status": "AVAILABLE", "version": "3.7c", "path": "/bin/tmux"},
                    "codex": {"status": "AVAILABLE", "version": "1", "path": "/bin/codex"},
                }},
            })()
            try:
                with patch.object(app, "runtime_preflight", return_value=fake):
                    result = app.product_runtime_preflight()
                self.assertIn("dataDirectory", result["checks"])
                self.assertIn("workspace", result["checks"])
                self.assertIn("productPort", result["checks"])
                self.assertFalse(result["optionalDependencies"]["gptWeb"]["blocksV1"])
                self.assertFalse(result["optionalDependencies"]["minimax"]["blocksV1"])
                self.assertFalse(result["optionalDependencies"]["openaiApi"]["blocksV1"])
                for item in result["checks"].values():
                    self.assertIn("code", item)
                    self.assertIn("nextAction", item)
            finally:
                app.close()

    def test_codex_auth_probe_uses_controlled_path_for_gui_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hermes_bin = root / ".hermes" / "node" / "bin"
            hermes_bin.mkdir(parents=True)
            fake = type("Preflight", (), {
                "cao_server_healthy": True,
                "as_dict": lambda self: {"tools": {
                    "cao": {"status": "AVAILABLE", "version": "2.5.0", "path": "/bin/cao"},
                    "tmux": {"status": "AVAILABLE", "version": "3.7c", "path": "/bin/tmux"},
                    "codex": {"status": "AVAILABLE", "version": "codex-cli 0.153.4", "path": str(hermes_bin / "codex")},
                }},
            })()
            app = None
            try:
                with patch.dict(os.environ, {
                    "HOME": str(root),
                    "PATH": "/finder/minimal/path",
                    "OPENAI_API_KEY": "must-not-be-forwarded",
                }, clear=True):
                    app = Phase2Application(SQLiteStore(root / "state.db"), adapter_factory=lambda **_: None)
                    with patch.object(app, "runtime_preflight", return_value=fake):
                        with patch("ai_meeting_room.product.app.subprocess.run", return_value=subprocess.CompletedProcess(
                            args=["codex", "login", "status"], returncode=0,
                            stdout="Logged in using ChatGPT", stderr="",
                        )) as run:
                            result = app.product_runtime_preflight()

                self.assertEqual(result["checks"]["codexAuth"]["status"], "READY")
                probe_env = run.call_args.kwargs["env"]
                self.assertIn(str(hermes_bin), probe_env["PATH"].split(os.pathsep))
                self.assertNotIn("OPENAI_API_KEY", probe_env)
                self.assertNotIn("must-not-be-forwarded", repr(probe_env))
            finally:
                if app is not None:
                    app.close()

    def test_cao_tool_version_probe_uses_product_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = Phase2Application(SQLiteStore(Path(directory) / "state.db"), adapter_factory=lambda **_: None)
            fake = object()
            try:
                with patch("ai_meeting_room.product.app.CaoRuntimePreflight") as constructor:
                    constructor.return_value.check.return_value = fake
                    self.assertIs(app.runtime_preflight(), fake)
                constructor.assert_called_once_with(
                    app.cao_base_url,
                    resolver=app.runtime_tool_resolver,
                )
            finally:
                app.close()

    def test_v1_ui_exposes_user_triggered_backup_restore_and_diagnostics(self) -> None:
        for marker in (
            "备份数据", "恢复备份", "导出诊断报告", "/api/system/backups",
            "/api/system/diagnostics", "desktopBrainStatusOptIn",
            "V1 默认使用手动 GPT 交接", "重新检查运行环境",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, UI_HTML)
        self.assertIn("确认用所选备份覆盖当前数据", UI_HTML)

    def test_final_v1_fixture_smoke_completes_and_recovers_without_duplicate_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            factory = lambda **_: V1SmokeAdapter()
            app = Phase2Application(SQLiteStore(database), adapter_factory=factory, monitor_poll_interval_seconds=0.01)
            try:
                meeting_id = app.create_meeting("TEST_SMOKE", directory)["meeting"]["meeting_id"]
                brain_participant_id = app.store.get_brain_participant(meeting_id)["participantId"]
                agent_id = app.join_provider(meeting_id, "codex")["agents"][0]["agent_id"]
                app.start_meeting(meeting_id)
                app.pause(meeting_id, "TEST_SMOKE pause/resume history")
                class HealthyResponse:
                    status = 200
                    def __enter__(self): return self
                    def __exit__(self, *_args): return None
                with patch("ai_meeting_room.product.app.urllib.request.urlopen", return_value=HealthyResponse()):
                    recovery = app.recover(meeting_id)
                self.assertTrue(recovery["recovered"])
                task_id = app.create_task(meeting_id, "TEST_SMOKE_TASK", "Use fixture only", agent_id)["tasks"][-1]["task_id"]
                app.dispatch_task(meeting_id, task_id)
                deadline = time.monotonic() + 3
                snapshot = app.snapshot(meeting_id)
                while time.monotonic() < deadline:
                    snapshot = app.snapshot(meeting_id)
                    if snapshot.get("brainHandoff"):
                        break
                    time.sleep(0.02)
                handoff = snapshot["brainHandoff"]
                request_id = handoff["requestId"]
                app.copy_manual_gpt_handoff(meeting_id, request_id)
                record = app.store.get_manual_gpt_handoff_request(request_id)
                packet = record["packet"]
                decision = {
                    "schemaVersion": "ai-meeting-room.brain-decision-packet.v1",
                    "decisionId": str(uuid.uuid4()),
                    "requestId": packet["requestId"],
                    "packetId": packet["packetId"],
                    "meetingId": packet["meetingId"],
                    "taskId": packet["taskId"],
                    "resultId": packet["resultId"],
                    "decision": "ACCEPT",
                    "reason": "TEST_SMOKE fixture accepted",
                    "createdAt": "2026-09-22T00:00:00+00:00",
                    "nonceEcho": packet["nonce"],
                }
                app.import_manual_gpt_decision(meeting_id, request_id, json.dumps(decision))
                app.apply_manual_gpt_decision(meeting_id, request_id)
                completed = app.complete_meeting(meeting_id, reason="TEST_SMOKE complete")
                self.assertEqual(completed["meeting"]["status"], "COMPLETED")
                summary_before = app.export_meeting_summary(meeting_id)["markdown"]
                self.assertIn("TEST_SMOKE", summary_before)
                self.assertIn("MeetingPaused", summary_before)
                self.assertIn("MeetingRecovering", summary_before)
                self.assertIn("MeetingStarted", summary_before)
                self.assertIsNone(app.snapshot(meeting_id)["brainHandoff"])
            finally:
                app.close()

            # Simulate the accepted pre-V1 database whose schema existed but
            # had no version marker; the formal startup migration must retain
            # the full meeting, task/result, Brain, decision and audit history.
            with sqlite3.connect(database) as db:
                db.execute("PRAGMA user_version = 0")
            restored = Phase2Application(SQLiteStore(database), adapter_factory=factory)
            try:
                snapshot = restored.snapshot(meeting_id)
                self.assertEqual(snapshot["meeting"]["status"], "COMPLETED")
                self.assertIsNone(snapshot["brainHandoff"])
                self.assertEqual(snapshot["tasks"][0]["status"], "COMPLETED")
                self.assertEqual(restored.store.get_brain_participant(meeting_id)["participantId"], brain_participant_id)
                requests = restored.store.list_manual_gpt_handoff_requests(meeting_id)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]["status"], "CONSUMED")
                self.assertTrue(restored.store.list_brain_decisions(meeting_id))
                event_types = {item["event_type"] for item in restored.store.list_events(meeting_id, limit=None)}
                self.assertIn("MeetingPaused", event_types)
                self.assertIn("MeetingRecovering", event_types)
                self.assertIn("MeetingStarted", event_types)
                self.assertEqual(restored.export_meeting_summary(meeting_id)["markdown"], summary_before)
                with sqlite3.connect(database) as db:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
