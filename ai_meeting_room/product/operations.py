"""V1 local data, backup, restore, logging, and diagnostic operations."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..data_paths import default_product_data_root, validate_database_path
from ..persistence.sqlite_store import SCHEMA_VERSION


BACKUP_FORMAT = "ai-meeting-room.backup.v1"
MAX_BACKUP_BYTES = 2 * 1024 * 1024 * 1024
MAX_BACKUP_MANIFEST_BYTES = 1024 * 1024
MAX_DIAGNOSTIC_LOG_LINES = 200
_SAFE_BACKUP_MEMBERS = {"manifest.json", "meeting.db"}
_FORBIDDEN_CONFIG_KEYS = {
    "authorization", "password", "credential", "apicredential", "apikey",
    "secret", "token", "accesstoken", "refreshtoken", "cookie",
}
_SECRET_LINE = re.compile(
    r"(?i)(authorization\s*:|cookie\s*=|password\s*=|api[_-]?key\s*[:=]|"
    r"refresh[_-]?token\s*[:=]|bearer\s+[A-Za-z0-9._~-]+|sk-[A-Za-z0-9_-]{12,})"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _without_credentials(value: Any) -> Any:
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in _FORBIDDEN_CONFIG_KEYS:
                continue
            safe[str(key)] = _without_credentials(item)
        return safe
    if isinstance(value, list):
        return [_without_credentials(item) for item in value]
    return value


@dataclass(frozen=True)
class ProductDataPaths:
    root: Path
    database: Path
    logs: Path
    backups: Path
    diagnostics: Path
    runtime: Path

    @classmethod
    def from_root(cls, root: str | Path) -> "ProductDataPaths":
        resolved = Path(root).expanduser().resolve()
        return cls(
            root=resolved,
            database=resolved / "meeting.db",
            logs=resolved / "logs",
            backups=resolved / "backups",
            diagnostics=resolved / "diagnostics",
            runtime=resolved / "runtime",
        )

    @classmethod
    def from_database(cls, database: str | Path) -> "ProductDataPaths":
        resolved_database = validate_database_path(database)
        paths = cls.from_root(resolved_database.parent)
        return cls(
            root=paths.root,
            database=resolved_database,
            logs=paths.logs,
            backups=paths.backups,
            diagnostics=paths.diagnostics,
            runtime=paths.runtime,
        )

    def validate_containment(self) -> None:
        root = self.root.resolve()
        for candidate in (self.database, self.logs, self.backups, self.diagnostics, self.runtime):
            try:
                candidate.resolve().relative_to(root)
            except ValueError as exc:
                from ..data_paths import ProductDataPathError
                raise ProductDataPathError("PRODUCT_PATH_ESCAPES_OVERRIDE_ROOT") from exc

    @classmethod
    def default(cls) -> "ProductDataPaths":
        return cls.from_root(default_product_data_root())

    def ensure(self) -> None:
        self.validate_containment()
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in (self.logs, self.backups, self.diagnostics, self.runtime):
            directory.mkdir(parents=True, exist_ok=True)
        self.validate_containment()


class BackupValidationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def configure_product_logging(paths: ProductDataPaths) -> logging.Logger:
    """Configure one bounded, local, metadata-only product log."""
    paths.validate_containment()
    paths.ensure()
    logger = logging.getLogger("ai_meeting_room.product")
    logger.setLevel(logging.INFO)
    target = str(paths.logs / "product.log")
    for existing in tuple(logger.handlers):
        if isinstance(existing, RotatingFileHandler) and existing.baseFilename != target:
            logger.removeHandler(existing)
            existing.close()
    if not any(isinstance(item, RotatingFileHandler) and item.baseFilename == target for item in logger.handlers):
        handler = RotatingFileHandler(target, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.propagate = False
    return logger


class V1Operations:
    def __init__(self, paths: ProductDataPaths, *, app_version: str) -> None:
        self.paths = paths
        self.app_version = app_version
        self.paths.ensure()

    def _backup_path(self, backup_id: str) -> Path:
        if not re.fullmatch(r"v1-[0-9TZ-]+-[0-9a-f]{8}", backup_id):
            raise BackupValidationError("BACKUP_ID_INVALID", "backup identity is invalid")
        candidate = (self.paths.backups / f"{backup_id}.aimr-backup").resolve()
        if candidate.parent != self.paths.backups.resolve():
            raise BackupValidationError("BACKUP_PATH_INVALID", "backup path escaped the backup directory")
        return candidate

    def create_backup(self) -> dict[str, Any]:
        if not self.paths.database.is_file():
            raise BackupValidationError("DATABASE_NOT_FOUND", "product database does not exist")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_id = f"v1-{stamp}-{uuid4().hex[:8]}"
        destination = self._backup_path(backup_id)
        with tempfile.TemporaryDirectory(dir=self.paths.runtime) as directory:
            snapshot = Path(directory) / "meeting.db"
            with sqlite3.connect(self.paths.database) as source, sqlite3.connect(snapshot) as target:
                source.backup(target)
            # The formal provider stores secrets in Keychain, not SQLite.
            # Scrub known legacy/raw credential keys from the snapshot anyway;
            # the live database is never mutated by this compatibility guard.
            with sqlite3.connect(snapshot) as target:
                row = target.execute(
                    "SELECT data FROM brain_provider_config WHERE config_id=1"
                ).fetchone()
                if row is not None:
                    try:
                        safe_config = _without_credentials(json.loads(row[0]))
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise BackupValidationError(
                            "PROVIDER_CONFIG_INVALID", "provider metadata could not be safely backed up"
                        ) from exc
                    target.execute(
                        "UPDATE brain_provider_config SET data=? WHERE config_id=1",
                        (json.dumps(safe_config, ensure_ascii=False, separators=(",", ":")),),
                    )
                    target.commit()
            database_sha = _sha256_file(snapshot)
            manifest = {
                "format": BACKUP_FORMAT,
                "backupId": backup_id,
                "createdAt": utc_now(),
                "appVersion": self.app_version,
                "schemaVersion": SCHEMA_VERSION,
                "databaseSha256": database_sha,
                "contents": ["meeting.db"],
                "excludesCredentials": True,
            }
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                bundle.write(snapshot, "meeting.db")
                bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2))
            temporary.replace(destination)
        return {**manifest, "path": str(destination), "status": "CREATED"}

    def list_backups(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for archive in sorted(self.paths.backups.glob("v1-*.aimr-backup"), reverse=True):
            try:
                item = self.validate_backup(archive.stem)
                results.append({key: item[key] for key in ("backupId", "createdAt", "appVersion", "schemaVersion", "status")})
            except BackupValidationError:
                results.append({"backupId": archive.stem, "status": "INVALID"})
        return results

    def validate_backup(self, backup_id: str) -> dict[str, Any]:
        archive = self._backup_path(backup_id)
        if not archive.is_file() or archive.stat().st_size > MAX_BACKUP_BYTES:
            raise BackupValidationError("BACKUP_NOT_FOUND_OR_TOO_LARGE", "backup is unavailable or too large")
        try:
            with zipfile.ZipFile(archive) as bundle:
                entries = bundle.infolist()
                names = [entry.filename for entry in entries]
                if len(names) != len(set(names)) or set(names) != _SAFE_BACKUP_MEMBERS:
                    raise BackupValidationError("BACKUP_CONTENTS_INVALID", "backup contains unexpected or duplicate files")
                sizes = {entry.filename: entry.file_size for entry in entries}
                if (
                    sizes.get("manifest.json", MAX_BACKUP_MANIFEST_BYTES + 1) > MAX_BACKUP_MANIFEST_BYTES
                    or sizes.get("meeting.db", MAX_BACKUP_BYTES + 1) > MAX_BACKUP_BYTES
                ):
                    raise BackupValidationError("BACKUP_CONTENTS_TOO_LARGE", "backup contents exceed safety limits")
                manifest = json.loads(bundle.read("manifest.json"))
                with tempfile.TemporaryDirectory(dir=self.paths.runtime) as directory:
                    probe = Path(directory) / "meeting.db"
                    digest = hashlib.sha256()
                    with bundle.open("meeting.db") as source, probe.open("wb") as target:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            target.write(chunk)
                            digest.update(chunk)
                    database_sha = digest.hexdigest()
                    probe_path = probe
                    if database_sha != manifest.get("databaseSha256"):
                        raise BackupValidationError("BACKUP_CHECKSUM_MISMATCH", "backup checksum does not match")
                    with sqlite3.connect(f"file:{probe_path}?mode=ro", uri=True) as db:
                        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise BackupValidationError("BACKUP_DATABASE_INVALID", "database integrity check failed")
                        if int(db.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_VERSION:
                            raise BackupValidationError("BACKUP_SCHEMA_INCOMPATIBLE", "database schema is incompatible")
        except BackupValidationError:
            raise
        except sqlite3.Error as exc:
            raise BackupValidationError("BACKUP_DATABASE_INVALID", "database could not be opened") from exc
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError, KeyError, AttributeError, TypeError) as exc:
            raise BackupValidationError("BACKUP_ARCHIVE_INVALID", "backup archive could not be validated") from exc
        if (
            manifest.get("format") != BACKUP_FORMAT
            or manifest.get("backupId") != backup_id
            or manifest.get("schemaVersion") != SCHEMA_VERSION
            or manifest.get("appVersion") != self.app_version
            or manifest.get("contents") != ["meeting.db"]
        ):
            raise BackupValidationError("BACKUP_MANIFEST_INCOMPATIBLE", "backup metadata is incompatible")
        return {**manifest, "path": str(archive), "status": "VALID"}

    def restore_backup(self, backup_id: str, *, confirm_overwrite: bool) -> dict[str, Any]:
        if not confirm_overwrite:
            raise BackupValidationError("RESTORE_CONFIRMATION_REQUIRED", "explicit overwrite confirmation is required")
        manifest = self.validate_backup(backup_id)
        archive = self._backup_path(backup_id)
        with tempfile.TemporaryDirectory(dir=self.paths.runtime) as directory:
            restored = Path(directory) / "meeting.db"
            with zipfile.ZipFile(archive) as bundle, bundle.open("meeting.db") as source, restored.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            temporary = self.paths.database.with_suffix(".restore.tmp")
            shutil.copy2(restored, temporary)
            temporary.replace(self.paths.database)
        for suffix in ("-wal", "-shm", "-journal"):
            self.paths.database.with_name(self.paths.database.name + suffix).unlink(missing_ok=True)
        return {
            "status": "RESTORED",
            "backupId": backup_id,
            "restoredAt": utc_now(),
            "databaseSha256": manifest["databaseSha256"],
            "restartRequired": True,
        }

    def create_diagnostic_report(
        self,
        *,
        preflight: dict[str, Any],
        meetings: list[dict[str, Any]],
        build: dict[str, Any],
    ) -> dict[str, Any]:
        recent: list[str] = []
        for log_name in ("product.log", "desktop.log", "brain-connect-debug.log"):
            product_log = self.paths.logs / log_name
            if product_log.is_file():
                lines = product_log.read_text(encoding="utf-8", errors="replace").splitlines()
                recent.extend(
                    f"[{log_name}] " + ("[REDACTED_SENSITIVE_LOG_LINE]" if _SECRET_LINE.search(line) else line[:2000])
                    for line in lines[-MAX_DIAGNOSTIC_LOG_LINES:]
                )
        recent = recent[-MAX_DIAGNOSTIC_LOG_LINES:]
        safe_meetings = [
            {"meetingId": item.get("meeting_id") or item.get("meetingId"), "status": item.get("status")}
            for item in meetings
        ]
        report = {
            "format": "ai-meeting-room.diagnostic.v1",
            "createdAt": utc_now(),
            "appVersion": self.app_version,
            "preflight": preflight,
            "meetings": safe_meetings,
            "recentLogs": recent,
            "build": build,
            "privacy": {
                "credentialsIncluded": False,
                "chatContentIncluded": False,
                "environmentIncluded": False,
            },
        }
        name = f"diagnostic-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}.json"
        destination = self.paths.diagnostics / name
        destination.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        return {"status": "CREATED", "path": str(destination), "createdAt": report["createdAt"]}
