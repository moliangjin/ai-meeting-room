"""Minimal developer control CLI; intentionally no GUI or web bridge."""

from __future__ import annotations

import argparse
import json
from .data_paths import resolve_product_paths_from_environment

from .core.service import MeetingCore
from .persistence.sqlite_store import SQLiteStore


def _store() -> SQLiteStore:
    return SQLiteStore(resolve_product_paths_from_environment().database)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai-meeting-room")
    sub = parser.add_subparsers(dest="command", required=True)
    meeting = sub.add_parser("meeting"); meeting_sub = meeting.add_subparsers(dest="action", required=True)
    create = meeting_sub.add_parser("create"); create.add_argument("name"); create.add_argument("--meeting-id")
    status = meeting_sub.add_parser("status"); status.add_argument("meeting_id")
    start = meeting_sub.add_parser("start"); start.add_argument("meeting_id")
    pause = meeting_sub.add_parser("pause"); pause.add_argument("meeting_id"); pause.add_argument("reason")
    recover = meeting_sub.add_parser("recover"); recover.add_argument("meeting_id")
    agent = sub.add_parser("agent"); agent_sub = agent.add_subparsers(dest="action", required=True)
    agent_list = agent_sub.add_parser("list"); agent_list.add_argument("meeting_id")
    agent_status = agent_sub.add_parser("status"); agent_status.add_argument("meeting_id"); agent_status.add_argument("agent_id")
    task = sub.add_parser("task"); task_sub = task.add_subparsers(dest="action", required=True)
    task_create = task_sub.add_parser("create"); task_create.add_argument("meeting_id"); task_create.add_argument("title"); task_create.add_argument("instruction")
    task_list = task_sub.add_parser("list"); task_list.add_argument("meeting_id")
    task_assign = task_sub.add_parser("assign"); task_assign.add_argument("meeting_id"); task_assign.add_argument("task_id"); task_assign.add_argument("agent_id")
    task_dispatch = task_sub.add_parser("dispatch"); task_dispatch.add_argument("meeting_id"); task_dispatch.add_argument("task_id")
    event = sub.add_parser("event"); event_sub = event.add_subparsers(dest="action", required=True)
    event_list = event_sub.add_parser("list"); event_list.add_argument("meeting_id")
    args = parser.parse_args(argv); store = _store()
    if args.command == "meeting" and args.action == "create":
        core = MeetingCore.create(args.name, store, args.meeting_id)
        print(json.dumps({"meetingId": core.meeting.meeting.meeting_id, "status": core.meeting.meeting.status.value}, ensure_ascii=False)); return 0
    if args.command == "meeting" and args.action == "status":
        print(json.dumps(store.get_meeting(args.meeting_id), ensure_ascii=False)); return 0
    if args.command == "meeting" and args.action == "start":
        core = MeetingCore.restore(args.meeting_id, store); core.start()
        print(json.dumps(store.get_meeting(args.meeting_id), ensure_ascii=False)); return 0
    if args.command == "meeting" and args.action == "pause":
        core = MeetingCore.restore(args.meeting_id, store); core.pause(args.reason)
        print(json.dumps(store.get_meeting(args.meeting_id), ensure_ascii=False)); return 0
    if args.command == "meeting" and args.action == "recover":
        core = MeetingCore.restore(args.meeting_id, store)
        result = core.recover(cao_health_check=lambda: False, workspace_health_check=lambda: False)
        print(json.dumps({"recovered": result, "meeting": store.get_meeting(args.meeting_id)}, ensure_ascii=False)); return 0
    if args.command == "agent" and args.action == "list":
        print(json.dumps(store.list_agents(args.meeting_id), ensure_ascii=False)); return 0
    if args.command == "agent" and args.action == "status":
        rows = [row for row in store.list_agents(args.meeting_id) if row["agent_id"] == args.agent_id]
        print(json.dumps(rows[0] if rows else None, ensure_ascii=False)); return 0
    if args.command == "task" and args.action == "create":
        core = MeetingCore.restore(args.meeting_id, store); task_record = core.create_task(args.title, args.instruction)
        print(json.dumps({"taskId": task_record.task_id, "status": task_record.status.value}, ensure_ascii=False)); return 0
    if args.command == "task" and args.action == "list":
        print(json.dumps(store.list_tasks(args.meeting_id), ensure_ascii=False)); return 0
    if args.command == "task" and args.action == "assign":
        core = MeetingCore.restore(args.meeting_id, store); record = core.tasks.assign_task(args.task_id, args.agent_id)
        print(json.dumps({"taskId": record.task_id, "status": record.status.value, "agentId": record.assigned_agent_id}, ensure_ascii=False)); return 0
    if args.command == "task" and args.action == "dispatch":
        core = MeetingCore.restore(args.meeting_id, store); record = core.tasks.dispatch_task(args.task_id)
        print(json.dumps({"taskId": record.task_id, "status": record.status.value}, ensure_ascii=False)); return 0
    if args.command == "event" and args.action == "list":
        print(json.dumps(store.list_events(args.meeting_id), ensure_ascii=False)); return 0
    parser.error("unsupported command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
