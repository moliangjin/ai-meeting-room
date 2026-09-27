"""Desktop Brain POC result validation.

The Electron WebContents owns browser interaction. This module only reuses the
existing strict BrainDecision parser and returns a transport-shaped result; it
does not touch MeetingCore, TaskEngine, BrainInbox, or persistence.
"""

from __future__ import annotations

from typing import Any

from .protocol import parse_brain_decision


DESKTOP_BRAIN_POC_TASK_ID = "desktop-brain-poc"
DESKTOP_BRAIN_POC_PROMPT = (
    "Return exactly one JSON object and no Markdown, no code fence, and no extra prose.\n\n"
    "Use exactly this schema and values:\n\n"
    '{\n  "decision": "ACCEPT",\n  "taskId": "desktop-brain-poc",\n'
    '  "reason": "desktop brain poc",\n  "instruction": "",\n  "confidence": 1.0\n}\n\n'
    "Do not discuss this request and do not modify any files."
)


def validate_desktop_brain_poc(raw_response: str, *, brain_request_id: str) -> dict[str, Any]:
    decision = parse_brain_decision(
        raw_response,
        brain_request_id=brain_request_id,
        expected_task_id=DESKTOP_BRAIN_POC_TASK_ID,
        allowed_decisions=("ACCEPT",),
    )
    return {
        "decision": {
            "decision": decision.decision,
            "taskId": decision.task_id,
            "reason": decision.reason,
            "instruction": decision.instruction,
            "confidence": decision.confidence,
        },
        "brainRequestId": decision.brain_request_id,
        "validated": True,
    }
