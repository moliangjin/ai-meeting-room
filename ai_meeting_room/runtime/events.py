"""Parser for MiniMax ``mcode exec --output-format stream-json`` events."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .errors import RuntimeErrorCode, RuntimeFailure


class RuntimeEventKind(str, Enum):
    STARTED = "exec.started"
    SESSION_STARTED = "session.started"
    TURN_STARTED = "turn.started"
    ITEM_STARTED = "item.started"
    ITEM_UPDATED = "item.updated"
    ITEM_COMPLETED = "item.completed"
    TURN_COMPLETED = "turn.completed"
    EXEC_COMPLETED = "exec.completed"
    ERROR = "error"


_KNOWN = {item.value for item in RuntimeEventKind}


@dataclass(frozen=True)
class RuntimeEvent:
    event_type: str
    sequence: int
    timestamp_ms: int | None
    run_id: str
    session_id: str | None
    turn_id: str | None
    payload: dict[str, Any]


def parse_stream_json_line(line: str) -> RuntimeEvent:
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeFailure(RuntimeErrorCode.UNKNOWN, "invalid stream-json line") from exc
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise RuntimeFailure(RuntimeErrorCode.UNKNOWN, "unsupported stream-json envelope")
    event_type = value.get("type")
    run_id = value.get("runId")
    sequence = value.get("sequence")
    if event_type not in _KNOWN or not isinstance(run_id, str) or not isinstance(sequence, int):
        raise RuntimeFailure(RuntimeErrorCode.UNKNOWN, "unknown or incomplete stream-json event")
    timestamp = value.get("timestampMs")
    if timestamp is not None and not isinstance(timestamp, int):
        raise RuntimeFailure(RuntimeErrorCode.UNKNOWN, "invalid event timestamp")
    return RuntimeEvent(event_type, sequence, timestamp, run_id, value.get("sessionId"), value.get("turnId"), value)


def extract_final_output(event: RuntimeEvent) -> str:
    if event.event_type != RuntimeEventKind.EXEC_COMPLETED.value:
        raise RuntimeFailure(RuntimeErrorCode.PROTOCOL_ERROR, "final output requested from non-terminal event")
    result = event.payload.get("result")
    if not isinstance(result, dict) or result.get("status") != "succeeded" or not isinstance(result.get("output"), str):
        raise RuntimeFailure(RuntimeErrorCode.UNKNOWN, "exec completed without a successful output")
    return result["output"]
