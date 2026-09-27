#!/usr/bin/env python3
"""Build the local macOS V1 app from the repository's existing Electron runtime."""

from __future__ import annotations

import plistlib
import re
import shutil
import argparse
import json
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "ai_meeting_room" / "VERSION").read_text(encoding="utf-8").strip()
ELECTRON_APP = ROOT / "desktop" / "node_modules" / "electron" / "dist" / "Electron.app"
OUTPUT = ROOT / "dist" / "AI Meeting Room V1.app"


def ignored(_directory: str, names: list[str]) -> set[str]:
    return {
        name for name in names
        if name in {
            "__pycache__", ".pytest_cache", ".DS_Store", ".git", ".venv", "tests", "test", "logs",
            "backups", "diagnostics", "workspaces", "dist", "build",
            "out", "node_modules", "chrome-brain-profile", "browser-profile",
        }
        or name.startswith(".env")
        or name.endswith((".pyc", ".log", ".db", ".sqlite", ".sqlite3", ".cookie", ".pem", ".key"))
    }


def validate_acceptance_config(config: dict[str, object]) -> dict[str, object]:
    expected_keys = {"schemaVersion", "acceptanceMode", "dataRoot", "productPort", "caoBaseUrl"}
    allowed_keys = expected_keys | {"agentSessionPrefix"}
    if not expected_keys.issubset(config) or not set(config).issubset(allowed_keys):
        raise ValueError("acceptance manifest fields are invalid")
    if config["schemaVersion"] != "ai-meeting-room.acceptance-config.v1" or config["acceptanceMode"] is not True:
        raise ValueError("acceptance manifest must explicitly enable acceptance mode")
    raw_root = config["dataRoot"]
    if not isinstance(raw_root, str) or not Path(raw_root).is_absolute():
        raise ValueError("acceptance data root must be an absolute path")
    sealed_base = Path("/private/tmp").resolve()
    data_root = Path(raw_root).expanduser().resolve()
    if data_root == sealed_base or sealed_base not in data_root.parents:
        raise ValueError("acceptance data root must be a dedicated child of /private/tmp")
    port = config["productPort"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("acceptance product port must be between 1 and 65535")
    raw_cao_url = config["caoBaseUrl"]
    if not isinstance(raw_cao_url, str):
        raise ValueError("acceptance CAO URL must be a local HTTP origin")
    try:
        cao_url = urlsplit(raw_cao_url)
        valid_local_cao = (
            cao_url.scheme == "http"
            and cao_url.hostname in {"127.0.0.1", "localhost", "::1"}
            and cao_url.username is None
            and cao_url.password is None
            and cao_url.path in {"", "/"}
            and not cao_url.query
            and not cao_url.fragment
            and (cao_url.port is None or 1 <= cao_url.port <= 65535)
        )
    except ValueError:
        valid_local_cao = False
    if not valid_local_cao:
        raise ValueError("acceptance CAO URL must be a local HTTP origin")
    prefix = config.get("agentSessionPrefix")
    if prefix is not None and (
        not isinstance(prefix, str)
        or re.fullmatch(r"aimr-v[0-9]+-r[0-9]+-[a-f0-9]{10}-", prefix) is None
    ):
        raise ValueError("acceptance Agent session prefix is invalid")
    normalized = {
        "schemaVersion": "ai-meeting-room.acceptance-config.v1",
        "acceptanceMode": True,
        "dataRoot": str(data_root),
        "productPort": port,
        "caoBaseUrl": raw_cao_url,
    }
    if prefix is not None:
        normalized["agentSessionPrefix"] = prefix
    return normalized


def build(output: str | Path = OUTPUT, acceptance_config: dict[str, object] | None = None) -> Path:
    if not ELECTRON_APP.is_dir():
        raise SystemExit("Electron runtime is not installed in desktop/node_modules")
    if acceptance_config is not None:
        acceptance_config = validate_acceptance_config(acceptance_config)
    destination = Path(output).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise SystemExit(f"refusing to overwrite existing package: {destination}")
    with tempfile.TemporaryDirectory(prefix="aimr-v1-package-", dir=destination.parent) as staging:
        temporary = Path(staging) / "AI Meeting Room.app"
        shutil.copytree(ELECTRON_APP, temporary, symlinks=True)
        resources = temporary / "Contents" / "Resources"
        bundled = resources / "app"
        if bundled.exists():
            shutil.rmtree(bundled)
        bundled.mkdir(parents=True)
        shutil.copytree(ROOT / "ai_meeting_room", bundled / "ai_meeting_room", ignore=ignored)
        shutil.copytree(ROOT / "phase0_poc", bundled / "phase0_poc", ignore=ignored)
        shutil.copytree(ROOT / "desktop", bundled / "desktop", ignore=ignored)
        shutil.copy2(ROOT / "ai_meeting_room" / "VERSION", bundled / "VERSION")
        if acceptance_config is not None:
            (resources / "aimr-acceptance-config.json").write_text(
                json.dumps(acceptance_config, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        package = bundled / "package.json"
        package.write_text(
            '{\n  "name": "ai-meeting-room",\n  "productName": "AI Meeting Room",\n'
            f'  "version": "{VERSION}",\n  "main": "desktop/main.js",\n  "private": true\n}}\n',
            encoding="utf-8",
        )
        source_modules = ROOT / "desktop" / "node_modules"
        destination_modules = bundled / "desktop" / "node_modules"
        shutil.copytree(
            source_modules,
            destination_modules,
            symlinks=True,
            ignore=lambda directory, names: ({"electron", ".bin"} if Path(directory) == source_modules else set()),
        )
        plist_path = temporary / "Contents" / "Info.plist"
        with plist_path.open("rb") as stream:
            plist = plistlib.load(stream)
        plist.update({
            "CFBundleDisplayName": "AI Meeting Room",
            "CFBundleName": "AI Meeting Room",
            "CFBundleIdentifier": "local.ai-meeting-room.desktop",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "AIMRAcceptanceBuild": acceptance_config is not None,
        })
        with plist_path.open("wb") as stream:
            plistlib.dump(plist, stream)
        temporary.rename(destination)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the local AI Meeting Room V1 app")
    parser.add_argument("--output", default=str(OUTPUT), help="new package destination; existing packages are never overwritten")
    parser.add_argument("--acceptance-data-root", help="embed an isolated clean-room root for an acceptance-only package")
    parser.add_argument("--acceptance-port", type=int, help="embed an isolated Product Shell port; requires --acceptance-data-root")
    parser.add_argument("--acceptance-cao-url", help="embed the local CAO URL; requires --acceptance-data-root")
    parser.add_argument("--acceptance-session-prefix", help="unique Agent session namespace for sealed acceptance builds")
    args = parser.parse_args()
    acceptance_options = (args.acceptance_data_root, args.acceptance_port, args.acceptance_cao_url, args.acceptance_session_prefix)
    if any(value is not None for value in acceptance_options) and not all(value is not None for value in acceptance_options):
        parser.error("acceptance package requires --acceptance-data-root, --acceptance-port, --acceptance-cao-url, and --acceptance-session-prefix")
    acceptance_config = None
    if args.acceptance_data_root is not None:
        if args.acceptance_port < 1 or args.acceptance_port > 65535:
            parser.error("--acceptance-port must be between 1 and 65535")
        acceptance_config = {
            "schemaVersion": "ai-meeting-room.acceptance-config.v1",
            "acceptanceMode": True,
            "dataRoot": str(Path(args.acceptance_data_root).expanduser().resolve()),
            "productPort": args.acceptance_port,
            "caoBaseUrl": args.acceptance_cao_url,
            "agentSessionPrefix": args.acceptance_session_prefix,
        }
    try:
        print(build(args.output, acceptance_config))
    except ValueError as error:
        parser.error(str(error))
