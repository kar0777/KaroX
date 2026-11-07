"""The `session` command group over the durable SessionStore."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any
from .cli_common import (
    emit as _emit,
    json_output_value as _json,
    write as _write,
)
from .models import AccessProfile
from .paths import session_dir
from .sessions import SessionStore

from .cli_present import _record_summary

def _handle_session(args: argparse.Namespace) -> int:
    store = SessionStore(session_dir())
    command = args.session_command
    if command == "create":
        record = store.create(
            args.repository.expanduser().resolve(strict=True),
            args.task,
            AccessProfile(args.access_profile),
            branch=args.branch,
            session_id=args.id,
            name=args.name,
        )
        _emit(_record_summary(record), json_output=args.json)
        return 0
    if command == "list":
        records = [
            _record_summary(item)
            for item in store.list(include_archived=bool(args.all))
        ]
        if args.json:
            _json(records)
        else:
            for item in records:
                label = item.get("name") or item["session_id"]
                archived = "\tarchived" if item.get("archived") else ""
                _write(
                    f"{item['session_id']}\t{label}\t{item['status']}\t"
                    f"{item['access_profile']}\t{item['repository']}{archived}\n"
                )
        return 0
    if command == "export":
        from .session_export import export_session

        record = store.load(args.session_id)
        text = export_session(record, args.format)
        target = args.output or Path.cwd() / f"karox-session-{record.session_id}.{args.format}"
        target = target.expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        _emit(
            {"session_id": record.session_id, "format": args.format, "path": str(target.resolve()), "bytes": len(text.encode("utf-8"))},
            json_output=args.json,
        )
        return 0
    if command == "fork":
        record = store.fork(
            args.session_id,
            session_id_new=args.new_session_id,
            task=args.task,
            name=args.name or "",
        )
        _emit(_record_summary(record), json_output=args.json)
        return 0
    if command == "rename":
        _emit(_record_summary(store.rename(args.session_id, args.name)), json_output=args.json)
        return 0
    if command == "archive":
        _emit(_record_summary(store.archive(args.session_id)), json_output=args.json)
        return 0
    if command == "unarchive":
        _emit(_record_summary(store.unarchive(args.session_id)), json_output=args.json)
        return 0
    if command == "delete":
        if args.confirm != args.session_id:
            raise ValueError("--confirm must exactly match the session ID")
        store.delete_archived(args.session_id)
        _emit({"session_id": args.session_id, "status": "deleted"}, json_output=args.json)
        return 0
    if command == "revoke":
        _emit(_record_summary(store.revoke(args.session_id)), json_output=args.json)
        return 0
    if command == "lock":
        record = store.load(args.session_id)
        lease_path = store.lease_path(args.session_id)
        info: dict[str, Any] = {
            "session_id": record.session_id,
            "locked": lease_path.exists(),
        }
        if lease_path.exists():
            try:
                raw = json.loads(lease_path.read_text(encoding="utf-8"))
                info["owner"] = raw.get("owner")
                info["expires_at"] = raw.get("expires_at")
                info["expired"] = float(raw.get("expires_at", 0)) < time.time()
            except (OSError, ValueError):
                info["locked"] = False
        _emit(info, json_output=args.json)
        return 0
    if command != "show":
        raise ValueError(f"unsupported session command: {command}")
    record = store.load(args.session_id)
    _emit(record.to_dict(), json_output=args.json)
    return 0
