"""Minimal SQLite persistence for meetings, agents, tasks, events and safety."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from ..data_paths import validate_database_path
from ..models import AgentRecord, DomainEvent, MeetingRecord, TaskRecord, model_dict


SCHEMA_VERSION = 1


class ManualGPTHandoffStoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _ClosingConnection(sqlite3.Connection):
    """Commit/rollback a context and deterministically close its connection."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


class SQLiteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = validate_database_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, factory=_ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            current_version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {current_version} is newer than supported schema {SCHEMA_VERSION}"
                )
            schema_script = """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS meetings (
                meeting_id TEXT PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agents (
                agent_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, event_type TEXT NOT NULL,
                timestamp TEXT NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS circuit_breakers (
                meeting_id TEXT PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS heartbeats (
                agent_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspaces (
                workspace_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS brain_events (
                event_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS brain_decisions (
                decision_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS brain_participants (
                meeting_id TEXT PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS brain_handoff_requests (
                request_id TEXT PRIMARY KEY,
                packet_id TEXT NOT NULL UNIQUE,
                meeting_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                result_id TEXT NOT NULL,
                status TEXT NOT NULL,
                staged_decision_id TEXT,
                claimed_decision_id TEXT,
                consumed_decision_id TEXT,
                updated_at TEXT NOT NULL,
                data TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_brain_handoff_consumed_decision
                ON brain_handoff_requests(consumed_decision_id)
                WHERE consumed_decision_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_brain_handoff_claimed_decision
                ON brain_handoff_requests(claimed_decision_id)
                WHERE claimed_decision_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_brain_handoff_staged_decision
                ON brain_handoff_requests(staged_decision_id)
                WHERE staged_decision_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS brain_provider_config (
                config_id INTEGER PRIMARY KEY CHECK (config_id = 1), data TEXT NOT NULL
            );
            """
            if current_version < SCHEMA_VERSION:
                schema_script += f"\nPRAGMA user_version = {SCHEMA_VERSION};"
            schema_script += "\nCOMMIT;"
            db.executescript(schema_script)

    def save_meeting(self, meeting: MeetingRecord) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO meetings VALUES (?, ?)", (meeting.meeting_id, json.dumps(model_dict(meeting))))

    def get_meeting(self, meeting_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT data FROM meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def list_meetings(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT data FROM meetings ORDER BY json_extract(data, '$.created_at') DESC").fetchall()
        return [json.loads(row["data"]) for row in rows]

    def save_agent(self, meeting_id: str, agent: AgentRecord) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO agents VALUES (?, ?, ?)", (agent.agent_id, meeting_id, json.dumps(model_dict(agent))))

    def list_agents(self, meeting_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT data FROM agents WHERE meeting_id=? ORDER BY agent_id", (meeting_id,)).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def save_task(self, task: TaskRecord) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?, ?)", (task.task_id, task.meeting_id, json.dumps(model_dict(task))))

    def list_tasks(self, meeting_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT data FROM tasks WHERE meeting_id=? ORDER BY task_id", (meeting_id,)).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def append_event(self, event: DomainEvent) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO events VALUES (?, ?, ?, ?, ?, ?)", (event.event_id, event.meeting_id, event.event_type, event.timestamp, event.source, json.dumps(event.payload)))

    def list_events(self, meeting_id: str, limit: int | None = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            if limit is None:
                rows = db.execute("SELECT * FROM events WHERE meeting_id=? ORDER BY timestamp DESC", (meeting_id,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM events WHERE meeting_id=? ORDER BY timestamp DESC LIMIT ?", (meeting_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def save_circuit(self, meeting_id: str, snapshot: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO circuit_breakers VALUES (?, ?)", (meeting_id, json.dumps(snapshot)))

    def get_circuit(self, meeting_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT data FROM circuit_breakers WHERE meeting_id=?", (meeting_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def save_heartbeat(self, meeting_id: str, agent_id: str, record: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO heartbeats VALUES (?, ?, ?)", (agent_id, meeting_id, json.dumps(record)))

    def save_brain_event(self, meeting_id: str, event: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO brain_events VALUES (?, ?, ?)", (event["eventId"], meeting_id, json.dumps(event)))

    def list_brain_events(self, meeting_id: str, limit: int | None = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            if limit is None:
                rows = db.execute("SELECT data FROM brain_events WHERE meeting_id=? ORDER BY rowid DESC", (meeting_id,)).fetchall()
            else:
                rows = db.execute("SELECT data FROM brain_events WHERE meeting_id=? ORDER BY rowid DESC LIMIT ?", (meeting_id, limit)).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def save_brain_decision(self, meeting_id: str, decision: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO brain_decisions VALUES (?, ?, ?)", (decision["decisionId"], meeting_id, json.dumps(decision)))

    def save_brain_participant(self, meeting_id: str, participant: dict[str, Any]) -> None:
        required = ("meetingId", "participantId", "role", "transport", "state", "joinedAt")
        if any(not isinstance(participant.get(key), str) or not participant[key] for key in required):
            raise ValueError("brain participant identity is incomplete")
        if participant["meetingId"] != meeting_id:
            raise ValueError("brain participant belongs to another Meeting")
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO brain_participants VALUES (?, ?)",
                (meeting_id, json.dumps(participant, ensure_ascii=False, separators=(",", ":"))),
            )

    def get_brain_participant(self, meeting_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT data FROM brain_participants WHERE meeting_id=?", (meeting_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def list_brain_decisions(self, meeting_id: str, limit: int | None = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            if limit is None:
                rows = db.execute("SELECT data FROM brain_decisions WHERE meeting_id=? ORDER BY rowid DESC", (meeting_id,)).fetchall()
            else:
                rows = db.execute("SELECT data FROM brain_decisions WHERE meeting_id=? ORDER BY rowid DESC LIMIT ?", (meeting_id, limit)).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def save_manual_gpt_handoff_request(self, record: dict[str, Any]) -> None:
        """Insert one immutable Brain Packet identity; retries must reuse it."""
        packet = record.get("packet")
        if not isinstance(packet, dict):
            raise ValueError("manual GPT handoff packet is required")
        required = ("requestId", "packetId", "meetingId", "taskId", "resultId")
        if any(not isinstance(packet.get(key), str) or not packet[key] for key in required):
            raise ValueError("manual GPT handoff identity is incomplete")
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as db:
            existing = db.execute(
                "SELECT data FROM brain_handoff_requests WHERE request_id=?",
                (packet["requestId"],),
            ).fetchone()
            if existing is not None:
                if json.loads(existing["data"]) != record:
                    raise ValueError("manual GPT handoff request identity conflict")
                return
            db.execute(
                "INSERT INTO brain_handoff_requests "
                "(request_id,packet_id,meeting_id,task_id,result_id,status,staged_decision_id,claimed_decision_id,consumed_decision_id,updated_at,data) "
                "VALUES (?,?,?,?,?,?,?,?,?,datetime('now'),?)",
                (
                    packet["requestId"], packet["packetId"], packet["meetingId"],
                    packet["taskId"], packet["resultId"], record.get("status", "WAITING_FOR_HUMAN_HANDOFF"),
                    record.get("stagedDecisionId"), record.get("claimedDecisionId"),
                    record.get("consumedDecisionId"), encoded,
                ),
            )

    def get_manual_gpt_handoff_request(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT data FROM brain_handoff_requests WHERE request_id=?", (request_id,),
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def find_manual_gpt_handoff_for_result(
        self, meeting_id: str, task_id: str, result_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT data FROM brain_handoff_requests WHERE meeting_id=? AND task_id=? AND result_id=? "
                "ORDER BY updated_at DESC,rowid DESC LIMIT 1",
                (meeting_id, task_id, result_id),
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def get_current_manual_gpt_handoff(self, meeting_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT data FROM brain_handoff_requests WHERE meeting_id=? "
                "AND status IN ('WAITING_FOR_HUMAN_HANDOFF','VALIDATED','APPLYING','APPLY_FAILED','BRAIN_PACKET_SENSITIVE_CONTENT_BLOCKED') "
                "ORDER BY updated_at DESC,rowid DESC LIMIT 1",
                (meeting_id,),
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def list_manual_gpt_handoff_requests(self, meeting_id: str) -> list[dict[str, Any]]:
        """Return persisted handoff records for recovery/audit without mutation."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT data FROM brain_handoff_requests WHERE meeting_id=? ORDER BY updated_at,rowid",
                (meeting_id,),
            ).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def mark_manual_gpt_handoff_copied(self, request_id: str, copied_at: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT status,data FROM brain_handoff_requests WHERE request_id=?", (request_id,),
            ).fetchone()
            if row is None:
                raise ManualGPTHandoffStoreError("STALE_DECISION_REJECTED")
            if row["status"] not in {"WAITING_FOR_HUMAN_HANDOFF", "VALIDATED"}:
                raise ManualGPTHandoffStoreError("DUPLICATE_DECISION_REJECTED")
            data = json.loads(row["data"])
            data["copyCount"] = int(data.get("copyCount", 0)) + 1
            data["lastCopiedAt"] = copied_at
            db.execute(
                "UPDATE brain_handoff_requests SET updated_at=?,data=? WHERE request_id=?",
                (copied_at, json.dumps(data, ensure_ascii=False, separators=(",", ":")), request_id),
            )
            return data

    def close_manual_gpt_handoff_by_operator(self, meeting_id: str, task_id: str, decision_id: str) -> dict[str, Any] | None:
        """Close a legacy, explicitly operator-authored decision without impersonating GPT."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT request_id,status,data FROM brain_handoff_requests WHERE meeting_id=? AND task_id=? "
                "AND status IN ('WAITING_FOR_HUMAN_HANDOFF','VALIDATED') ORDER BY updated_at DESC,rowid DESC LIMIT 1",
                (meeting_id, task_id),
            ).fetchone()
            if row is None:
                return None
            data = json.loads(row["data"])
            data["status"] = "OPERATOR_DECISION_APPLIED"
            data["operatorDecisionId"] = decision_id
            db.execute(
                "UPDATE brain_handoff_requests SET status='OPERATOR_DECISION_APPLIED',updated_at=datetime('now'),data=? WHERE request_id=?",
                (json.dumps(data, ensure_ascii=False, separators=(",", ":")), row["request_id"]),
            )
            return data

    def record_manual_gpt_validation_error(self, request_id: str, error_code: str) -> None:
        with self._connect() as db:
            row = db.execute(
                "SELECT status,data FROM brain_handoff_requests WHERE request_id=?", (request_id,),
            ).fetchone()
            if row is None or row["status"] != "WAITING_FOR_HUMAN_HANDOFF":
                return
            data = json.loads(row["data"])
            data["validationStatus"] = "INVALID"
            data["validationErrorCode"] = error_code
            db.execute(
                "UPDATE brain_handoff_requests SET updated_at=datetime('now'),data=? WHERE request_id=?",
                (json.dumps(data, ensure_ascii=False, separators=(",", ":")), request_id),
            )

    def stage_manual_gpt_decision(self, request_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        decision_id = decision.get("decisionId")
        if not isinstance(decision_id, str) or not decision_id:
            raise ManualGPTHandoffStoreError("INVALID_BRAIN_DECISION")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,data FROM brain_handoff_requests WHERE request_id=?", (request_id,),
            ).fetchone()
            if row is None:
                raise ManualGPTHandoffStoreError("STALE_DECISION_REJECTED")
            data = json.loads(row["data"])
            if row["status"] != "WAITING_FOR_HUMAN_HANDOFF":
                raise ManualGPTHandoffStoreError("DUPLICATE_DECISION_REJECTED")
            duplicate = db.execute(
                "SELECT 1 FROM brain_handoff_requests WHERE staged_decision_id=? "
                "OR claimed_decision_id=? OR consumed_decision_id=? LIMIT 1",
                (decision_id, decision_id, decision_id),
            ).fetchone()
            if duplicate is not None:
                raise ManualGPTHandoffStoreError("DUPLICATE_DECISION_REJECTED")
            data["status"] = "VALIDATED"
            data["validationStatus"] = "VALID"
            data["validationErrorCode"] = None
            data["stagedDecision"] = decision
            data["stagedDecisionId"] = decision_id
            db.execute(
                "UPDATE brain_handoff_requests SET status='VALIDATED',staged_decision_id=?,updated_at=datetime('now'),data=? WHERE request_id=?",
                (decision_id, json.dumps(data, ensure_ascii=False, separators=(",", ":")), request_id),
            )
            return data

    def claim_manual_gpt_decision(self, request_id: str, decision_id: str) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,staged_decision_id,claimed_decision_id,consumed_decision_id,data "
                "FROM brain_handoff_requests WHERE request_id=?", (request_id,),
            ).fetchone()
            if row is None:
                raise ManualGPTHandoffStoreError("STALE_DECISION_REJECTED")
            already_claimed = db.execute(
                "SELECT 1 FROM brain_handoff_requests WHERE claimed_decision_id=? OR consumed_decision_id=? LIMIT 1",
                (decision_id, decision_id),
            ).fetchone()
            if already_claimed is not None:
                raise ManualGPTHandoffStoreError("DUPLICATE_DECISION_REJECTED")
            if row["status"] != "VALIDATED" or row["staged_decision_id"] != decision_id:
                raise ManualGPTHandoffStoreError("STALE_DECISION_REJECTED")
            data = json.loads(row["data"])
            data["status"] = "APPLYING"
            data["claimedDecisionId"] = decision_id
            db.execute(
                "UPDATE brain_handoff_requests SET status='APPLYING',claimed_decision_id=?,updated_at=datetime('now'),data=? WHERE request_id=?",
                (decision_id, json.dumps(data, ensure_ascii=False, separators=(",", ":")), request_id),
            )
            return data

    def complete_manual_gpt_decision(self, request_id: str, decision_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT status,claimed_decision_id,data FROM brain_handoff_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None or row["status"] != "APPLYING" or row["claimed_decision_id"] != decision_id:
                raise ManualGPTHandoffStoreError("STALE_DECISION_REJECTED")
            data = json.loads(row["data"])
            data["status"] = "CONSUMED"
            data["consumedDecisionId"] = decision_id
            db.execute(
                "UPDATE brain_handoff_requests SET status='CONSUMED',consumed_decision_id=?,updated_at=datetime('now'),data=? WHERE request_id=?",
                (decision_id, json.dumps(data, ensure_ascii=False, separators=(",", ":")), request_id),
            )
            return data

    def fail_manual_gpt_decision(self, request_id: str, decision_id: str, error_code: str) -> None:
        with self._connect() as db:
            row = db.execute(
                "SELECT status,claimed_decision_id,data FROM brain_handoff_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None or row["status"] != "APPLYING" or row["claimed_decision_id"] != decision_id:
                return
            data = json.loads(row["data"])
            data["status"] = "APPLY_FAILED"
            data["applyErrorCode"] = error_code
            db.execute(
                "UPDATE brain_handoff_requests SET status='APPLY_FAILED',updated_at=datetime('now'),data=? WHERE request_id=?",
                (json.dumps(data, ensure_ascii=False, separators=(",", ":")), request_id),
            )

    def save_brain_provider_config(self, data: dict[str, Any]) -> None:
        """Persist metadata only; callers must never pass a credential value."""
        forbidden = {"apiCredential", "credential", "apiKey", "secret", "token", "cookie"}
        if forbidden.intersection(data):
            raise ValueError("secret fields are not allowed in provider config")
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO brain_provider_config VALUES (1, ?)", (json.dumps(data),))

    def get_brain_provider_config(self) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT data FROM brain_provider_config WHERE config_id=1").fetchone()
        return json.loads(row["data"]) if row else None

    def save_workspace(self, meeting_id: str, workspace_id: str, data: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO workspaces VALUES (?, ?, ?)", (workspace_id, meeting_id, json.dumps(data)))

    def health_check(self) -> bool:
        try:
            with self._connect() as db:
                db.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
