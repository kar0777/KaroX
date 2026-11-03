"""Pure text and list builders for everything the transcript and pickers show.

Nothing here touches a screen: each function turns a CLI ``--json`` payload or a
parsed session record into display text or picker items, so the exact output
can be asserted without running the app.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from rich.text import Text

from .commands import COMMANDS
from .i18n import tr
from .protocol import SessionState
from .widgets import DIM, GOLD, GREEN, MUTED, RED, TEXT, MenuItem


def clock(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not value:
        return "?"
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(value)))
    except (OverflowError, OSError, ValueError):
        return "?"


def token_text(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _flat(value: Any, limit: int = 0) -> str:
    text = " ".join(str(value or "").split())
    if limit and len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _price(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") if value else "0"


def same_repository(value: Any, repository: Path) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return os.path.normcase(str(Path(value).resolve())) == os.path.normcase(str(repository))
    except OSError:
        return False


# ------------------------------------------------------------------- blocks


def lines_text(lines: Iterable[tuple[str, str]]) -> Text:
    pieces: list[tuple[str, str]] = []
    for index, (text, style) in enumerate(lines):
        if index:
            pieces.append(("\n", style))
        pieces.append((text, style))
    return Text.assemble(*pieces) if pieces else Text("")


def help_text(lang: str) -> Text:
    lines: list[tuple[str, str]] = []
    width = max(len(command.name) + (len(command.argument) + 1 if command.argument else 0) for command in COMMANDS)
    for command in COMMANDS:
        usage = f"/{command.name}" + (f" {command.argument}" if command.argument else "")
        aliases = ""
        if command.aliases:
            aliases = "  (" + ", ".join(f"/{item}" for item in command.aliases) + ")"
        lines.append((f"{usage:<{width + 2}} {tr(lang, command.description)}{aliases}", TEXT))
    lines.append(("", MUTED))
    lines.append((tr(lang, "help.keys_title"), f"bold {GOLD}"))
    for line in tr(lang, "help.keys").splitlines():
        lines.append((line, MUTED))
    return lines_text(lines)


def status_text(
    *,
    lang: str,
    repository: Path,
    branch: str,
    model: str,
    session_id: str,
    effort: str,
    mode: str,
    tokens: int,
    verification: Sequence[Sequence[str]],
    suggested: Sequence[Sequence[str]],
    counters: dict[str, int],
    checkpoint_at: float | None,
) -> Text:
    none = tr(lang, "info.none")
    lines = [
        (f"{tr(lang, 'info.repository')}: {repository}", TEXT),
        (f"{tr(lang, 'info.branch')}: {branch or '-'}", TEXT),
        (f"{tr(lang, 'info.model')}: {model or tr(lang, 'status.no_model')}", TEXT if model else RED),
        (f"{tr(lang, 'info.session')}: {session_id}", TEXT),
        (tr(lang, "info.settings", effort=effort, mode=mode, language=lang), MUTED),
        (f"{tr(lang, 'info.tokens')}: {tokens:,}", MUTED),
        (
            f"{tr(lang, 'info.verification')}: "
            + ("; ".join(" ".join(item) for item in verification) or none),
            TEXT if verification else MUTED,
        ),
    ]
    if not verification and suggested:
        lines.append(
            (f"{tr(lang, 'info.suggested')}: " + "; ".join(" ".join(item) for item in suggested), DIM)
        )
    lines.append((tr(lang, "info.run_counters", **counters), MUTED))
    if checkpoint_at:
        lines.append((tr(lang, "info.checkpoint", when=clock(checkpoint_at)), MUTED))
    else:
        lines.append((tr(lang, "info.no_checkpoint"), DIM))
    return lines_text(lines)


def session_state_lines(state: SessionState | None, lang: str) -> list[tuple[str, str]]:
    """The durable record as display lines: every state the run persists."""

    lines: list[tuple[str, str]] = []
    if state is None or not state.session_id:
        lines.append((tr(lang, "session.empty"), MUTED))
        return lines

    def section(key: str) -> None:
        if lines:
            lines.append(("", MUTED))
        lines.append((tr(lang, key), f"bold {GOLD}"))

    def value(text: str, style: str = TEXT) -> None:
        if text:
            lines.append(("  " + text, style))

    section("session.section.meta")
    meta = state.session_id
    if state.name:
        meta += f" · {state.name}"
    meta += f" · {state.status or '?'} · {state.phase or '?'}"
    value(meta)
    detail = tr(lang, "session.meta_detail", revision=state.revision, when=clock(state.updated_at))
    if state.branch:
        detail += " · " + tr(lang, "session.meta_branch", branch=state.branch)
    value(detail, MUTED)

    section("session.section.task")
    value(_flat(state.task) or "—")
    if len(state.task_history) > 1:
        value(tr(lang, "session.history", count=len(state.task_history) - 1), DIM)

    section("session.section.plan")
    todo, doing, done = state.plan_tally
    if state.plan_items:
        value(tr(lang, "session.plan_tally", todo=todo, doing=doing, done=done), MUTED)
        for item in state.plan_items[:20]:
            status = str(item.get("status") or "todo")
            icon = {"done": "✓", "doing": "●"}.get(status, "○")
            colour = {"done": GREEN, "doing": GOLD}.get(status, MUTED)
            value(f"{icon} {_flat(item.get('text'), 70)}", colour)
    else:
        value(tr(lang, "session.none"), DIM)

    section("session.section.checkpoints")
    if state.has_checkpoint:
        value(
            tr(
                lang,
                "session.checkpoints_line",
                count=state.checkpoint_count,
                when=clock(state.last_checkpoint_at),
            )
        )
        last = state.checkpoints[-1] if state.checkpoints else {}
        tracked = last.get("tracked_count")
        untracked = last.get("untracked_count")
        if isinstance(tracked, int) or isinstance(untracked, int):
            value(
                tr(
                    lang,
                    "session.checkpoint_files",
                    tracked=tracked if isinstance(tracked, int) else 0,
                    untracked=untracked if isinstance(untracked, int) else 0,
                ),
                DIM,
            )
    else:
        value(tr(lang, "session.none"), DIM)

    section("session.section.verification")
    passed, total = state.check_tally
    if total:
        value(
            tr(lang, "session.checks_line", passed=passed, total=total),
            GREEN if passed == total else RED,
        )
        for check in state.checks[-4:]:
            argv = check.get("argv")
            command = " ".join(str(part) for part in argv) if isinstance(argv, list) else "?"
            ok = check.get("ok")
            icon = "✓" if ok is True else "✗" if ok is False else "?"
            exit_code = check.get("exit_code")
            suffix = f" · exit {exit_code}" if isinstance(exit_code, int) else ""
            value(f"{icon} {command}{suffix}", GREEN if ok is True else RED)
        if state.verified_step:
            value(tr(lang, "session.verified_at", step=state.verified_step), GREEN)
    else:
        value(tr(lang, "session.none"), DIM)

    section("session.section.provider")
    value(
        tr(
            lang,
            "session.provider_line",
            requests=state.requests,
            tokens=f"{state.token_total:,}",
        )
    )
    if state.costs:
        costs = " · ".join(
            f"{currency} {_price(amount)}" for currency, amount in sorted(state.costs.items())
        )
        value(tr(lang, "session.cost", cost=costs), MUTED)
    if state.cost_unknown:
        value(tr(lang, "session.cost_unknown", reason=state.cost_unknown), MUTED)

    section("session.section.mcp")
    value(
        ", ".join(state.mcp_server_ids) or tr(lang, "session.none"),
        TEXT if state.mcp_server_ids else DIM,
    )

    section("session.section.skills")
    value(
        ", ".join(state.skill_names) or tr(lang, "session.none"),
        TEXT if state.skill_names else DIM,
    )

    section("session.section.files")
    if state.changed_files:
        value(tr(lang, "session.files_line", count=len(state.changed_files)), MUTED)
        for path in state.changed_files[:8]:
            value(f"· {path}")
    else:
        value(tr(lang, "session.none"), DIM)
    return lines


def skill_text(skill: dict[str, Any], lang: str) -> Text:
    lines: list[tuple[str, str]] = []
    description = str(skill.get("description") or "")
    if description:
        lines.append((description, TEXT))
    version = str(skill.get("version") or "")
    if version:
        lines.append((f"{tr(lang, 'skill.version')}: {version}", MUTED))
    when = str(skill.get("when_to_use") or "")
    if when:
        lines.append((f"{tr(lang, 'skill.when_to_use')}: {when}", TEXT))
    permissions = skill.get("permissions")
    if isinstance(permissions, list) and permissions:
        names = ", ".join(str(item) for item in permissions if item)
        lines.append((f"{tr(lang, 'skill.permissions')}: {names}", MUTED))
    files = skill.get("files")
    if isinstance(files, list) and files:
        names = ", ".join(str(item) for item in files if item)
        lines.append((f"{tr(lang, 'skill.files')}: {names}", MUTED))
    return lines_text(lines or [("-", MUTED)])


def doctor_text(report: dict[str, Any]) -> tuple[Text, str]:
    """The doctor report as lines plus the notice level it deserves."""

    status = str(report.get("status") or "unknown")
    lines: list[tuple[str, str]] = [(f"karox: {status}", GREEN if status == "ok" else GOLD)]
    checks = report.get("checks")
    if isinstance(checks, dict):
        for name, check in checks.items():
            state = str(check.get("status") or "unknown") if isinstance(check, dict) else "unknown"
            detail = ""
            if isinstance(check, dict):
                detail = _flat(check.get("detail") or check.get("reason") or "", 80)
            colour = GREEN if state == "ok" else (RED if state in {"error", "failed"} else GOLD)
            mark = "✓" if state == "ok" else "!"
            lines.append((f"{mark} {name}: {state}" + (f" — {detail}" if detail else ""), colour))
    return lines_text(lines), ("success" if status == "ok" else "warning")


# ------------------------------------------------------------------ pickers


def model_items(models: Sequence[dict[str, Any]], lang: str) -> tuple[list[MenuItem], str | None]:
    items: list[MenuItem] = []
    current: str | None = None
    for item in models:
        provider = str(item.get("provider_id") or "")
        model = str(item.get("model_id") or "")
        if not provider or not model:
            continue
        key = f"{provider}/{model}"
        details: list[str] = []
        window = item.get("context_window")
        if isinstance(window, int) and not isinstance(window, bool) and window > 0:
            details.append(tr(lang, "models.context", tokens=token_text(window)))
        pricing = item.get("pricing")
        if isinstance(pricing, dict) and isinstance(pricing.get("input_per_million"), (int, float)):
            details.append(
                tr(
                    lang,
                    "models.price",
                    input=_price(float(pricing.get("input_per_million") or 0)),
                    output=_price(float(pricing.get("output_per_million") or 0)),
                )
            )
        else:
            details.append(tr(lang, "models.no_price"))
        selected = bool(item.get("selected"))
        if selected:
            current = key
            details.insert(0, tr(lang, "models.current"))
        display = item.get("display_name")
        search = f"{key} {display or ''}"
        items.append(MenuItem(key, key, " · ".join(details), search=search, marked=selected))
    return items, current


def session_items(
    sessions: Sequence[dict[str, Any]], repository: Path, current_id: str, lang: str
) -> list[MenuItem]:
    items: list[MenuItem] = []
    ordered = sorted(
        (item for item in sessions if isinstance(item, dict)),
        key=lambda item: float(item.get("updated_at") or 0) if isinstance(item.get("updated_at"), (int, float)) else 0.0,
        reverse=True,
    )
    for item in ordered:
        if not same_repository(item.get("repository"), repository):
            continue
        session_id = str(item.get("session_id") or "")
        if not session_id:
            continue
        task = _flat(item.get("task"), 60)
        label = _flat(item.get("name"), 40) or task or session_id
        details = [str(item.get("status") or "?"), clock(item.get("updated_at")), session_id[:24]]
        if label == task:
            task = ""
        if task:
            details.insert(0, task)
        marked = session_id == current_id
        if marked:
            details.insert(0, tr(lang, "sessions.current"))
        items.append(MenuItem(session_id, label, " · ".join(details), search=f"{label} {task} {session_id}", marked=marked))
    return items


CUSTOM_PROVIDER_KEY = "__custom__"
ADD_SERVER_KEY = "__add__"


def catalog_items(rows: Sequence[dict[str, Any]], lang: str) -> list[MenuItem]:
    connected: list[MenuItem] = []
    others: list[MenuItem] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("installable", True):
            continue
        provider_id = str(row.get("provider_id") or "")
        if not provider_id:
            continue
        name = str(row.get("display_name") or provider_id)
        details = [provider_id, tr(lang, "connect.models", count=int(row.get("model_count") or 0))]
        env = [str(item) for item in row.get("env") or [] if item]
        is_connected = bool(row.get("connected_as"))
        if is_connected:
            details.insert(0, tr(lang, "connect.connected"))
        elif env:
            details.append(tr(lang, "connect.key_env", env=env[0]))
        else:
            details.append(tr(lang, "connect.no_key"))
        item = MenuItem(provider_id, name, " · ".join(details), search=f"{name} {provider_id}", marked=is_connected)
        (connected if is_connected else others).append(item)
    items = connected + others
    items.append(MenuItem(CUSTOM_PROVIDER_KEY, tr(lang, "connect.custom"), tr(lang, "connect.custom_detail")))
    return items


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def mcp_items(
    payload: dict[str, Any],
    lang: str,
    *,
    pending: Mapping[str, str] | None = None,
) -> list[MenuItem]:
    """The MCP picker: each server with its transport, session state and reach.

    ``pending`` holds the servers chosen before this window's session exists;
    they are attached by the first run, so the row says so instead of "not in
    this session".
    """

    waiting = dict(pending or {})
    items: list[MenuItem] = []
    for entry in payload.get("servers", []) if isinstance(payload, dict) else []:
        if not isinstance(entry, dict):
            continue
        server = str(entry.get("server_id") or "")
        if not server:
            continue
        liveness = _mapping(entry.get("liveness"))
        selection = _mapping(entry.get("selection"))
        allowed = _mapping(selection.get("counts")).get("allowed", 0)
        state = str(selection.get("state") or "not_selected")
        if server in waiting:
            session_word = tr(lang, "mcp.state.pending")
        elif state == "current":
            session_word = tr(lang, "mcp.state.current", count=allowed)
        elif state == "stale":
            session_word = tr(lang, "mcp.state.stale")
        else:
            session_word = tr(lang, "mcp.state.not_selected")
        live = str(liveness.get("state") or "not_probed")
        live_key = f"mcp.live.{live}"
        live_word = tr(lang, live_key)
        details = [str(entry.get("transport") or "?"), session_word]
        details.append(live if live_word == live_key else live_word)
        if _mapping(entry.get("credential")).get("state") == "missing":
            details.append(tr(lang, "mcp.cred.missing"))
        items.append(MenuItem(server, server, " · ".join(details)))
    items.append(MenuItem(ADD_SERVER_KEY, tr(lang, "mcp.add_item"), tr(lang, "mcp.add_detail")))
    return items


__all__ = [
    "ADD_SERVER_KEY",
    "CUSTOM_PROVIDER_KEY",
    "catalog_items",
    "clock",
    "doctor_text",
    "help_text",
    "lines_text",
    "mcp_items",
    "model_items",
    "same_repository",
    "session_items",
    "session_state_lines",
    "skill_text",
    "status_text",
    "token_text",
]
