"""Stable, user-owned local data directory resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class ProductDataPathError(ValueError):
    """A configured product data path is invalid or escapes its explicit root."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ResolvedProductPaths:
    root: Path
    database: Path
    logs: Path
    backups: Path
    diagnostics: Path
    runtime: Path

    def validate_containment(self) -> "ResolvedProductPaths":
        root = self.root.resolve()
        for candidate in (self.database, self.logs, self.backups, self.diagnostics, self.runtime):
            resolved = candidate.resolve()
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise ProductDataPathError("PRODUCT_PATH_ESCAPES_OVERRIDE_ROOT") from exc
        return self


def resolve_product_paths_from_environment(environ: Mapping[str, str] | None = None) -> ResolvedProductPaths:
    """Resolve every persistent product path from one environment snapshot.

    An explicit data root is authoritative. A database override may not escape
    it; rejecting that combination before directory creation prevents a
    packaged clean-room run from silently touching a user's normal database.
    """
    values = os.environ if environ is None else environ
    configured_root = values.get("AI_MEETING_ROOM_DATA_DIR")
    configured_database = values.get("AI_MEETING_ROOM_DB")
    acceptance_mode = values.get("AI_MEETING_ROOM_ACCEPTANCE_MODE") == "1"
    if acceptance_mode and not configured_root:
        raise ProductDataPathError("PRODUCT_DATA_ROOT_OVERRIDE_REQUIRED")
    if configured_root:
        root = Path(configured_root).expanduser().resolve()
        database = (root / "meeting.db").resolve()
        if configured_database:
            requested_database = Path(configured_database).expanduser().resolve()
            if requested_database != database:
                raise ValueError("AI_MEETING_ROOM_DB_OUTSIDE_DATA_DIR")
    elif configured_database:
        database = Path(configured_database).expanduser().resolve()
        root = database.parent
    else:
        root = (Path.home() / ".ai-meeting-room").resolve()
        database = root / "meeting.db"
    return ResolvedProductPaths(
        root=root,
        database=database,
        logs=root / "logs",
        backups=root / "backups",
        diagnostics=root / "diagnostics",
        runtime=root / "runtime",
    ).validate_containment()


def validate_database_path(database: str | Path, environ: Mapping[str, str] | None = None) -> Path:
    """Validate a DB target against an explicit product root before opening it."""
    values = os.environ if environ is None else environ
    paths = resolve_product_paths_from_environment(values)
    resolved = Path(database).expanduser().resolve()
    if values.get("AI_MEETING_ROOM_DATA_DIR") or values.get("AI_MEETING_ROOM_ACCEPTANCE_MODE") == "1":
        if resolved != paths.database:
            raise ProductDataPathError("PRODUCT_PATH_ESCAPES_OVERRIDE_ROOT")
    return resolved


def default_product_data_root(environ: Mapping[str, str] | None = None) -> Path:
    """Return the configured V1 data root, outside source and package trees."""
    values = os.environ if environ is None else environ
    return resolve_product_paths_from_environment(values).root
