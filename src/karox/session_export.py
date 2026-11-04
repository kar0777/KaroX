"""Readable exports of one stored session: a self-contained HTML page or Markdown.

``karox session export`` turns the durable record into something a person can
open, share or attach to an issue: the task, outcome, checks, changed files,
usage and the whole trajectory -- each model turn with its tool calls and each
tool result -- in order.

* Everything passes through :func:`karox.security.redact` (display form) on
  the way out, on top of the redaction already applied when the session was
  stored, so a secret-shaped string that slipped into a tool result is not
  published by an export.
* The page has no scripts, no external fonts or images and no network
  references; long blocks fold with ``<details>``.
* Very long tool output is clamped (head and tail kept) with the omitted size
  stated, so a multi-megabyte log does not produce an unopenable page.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .security import redact

MAX_BLOCK_CHARACTERS = 12_000
_KIND_LABELS = {
    "task": "Task",
    "engine_note": "KaroX note",
    "engine_observation": "KaroX observation",
    "engine_repair": "KaroX repair prompt",
}


@dataclass(frozen=True)
class TimelineItem:
    role: str
    title: str
    text: str
    calls: tuple[tuple[str, str], ...] = ()
    reasoning: str = ""
    meta: str = ""
    error: bool = False


def _clamp(text: str, limit: int = MAX_BLOCK_CHARACTERS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit * 2 // 3]
    tail = text[-limit // 3 :]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n… [{omitted} characters omitted from the export] …\n{tail}"


def _safe(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    return _clamp(str(redact(value)))


def _pretty_json(text: str) -> str:
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return text


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) if value > 0 else 0


def _when(value: Any) -> str:
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    return _dt.datetime.fromtimestamp(stamp, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _usage_meta(entry: Mapping[str, Any]) -> str:
    parts: List[str] = []
    model = entry.get("model")
    if model:
        parts.append(str(model))
    usage = entry.get("usage") if isinstance(entry.get("usage"), Mapping) else {}
    for keys, label in (
        (("prompt_tokens", "input_tokens"), "in"),
        (("completion_tokens", "output_tokens"), "out"),
        (("cache_read_tokens", "cache_read_input_tokens", "cached_tokens"), "cache read"),
    ):
        value = next((usage.get(key) for key in keys if _count(usage.get(key))), None)
        if value:
            parts.append(f"{label} {int(value)}")
    cache = entry.get("prompt_cache") if isinstance(entry.get("prompt_cache"), Mapping) else {}
    cause = cache.get("cause")
    if isinstance(cause, str) and cause and cause != "hit":
        parts.append(f"cache: {cause}")
    return " · ".join(parts)


def timeline(record: Mapping[str, Any]) -> List[TimelineItem]:
    items: List[TimelineItem] = []
    history = record.get("provider_history")
    for entry in history if isinstance(history, list) else []:
        if not isinstance(entry, Mapping):
            continue
        role = str(entry.get("role") or "")
        if role == "assistant":
            calls = []
            for call in entry.get("tool_calls") or []:
                if isinstance(call, Mapping):
                    arguments = call.get("raw_arguments")
                    calls.append(
                        (
                            str(call.get("name") or "tool"),
                            _safe(_pretty_json(arguments) if isinstance(arguments, str) else arguments),
                        )
                    )
            items.append(
                TimelineItem(
                    "assistant",
                    "Model",
                    _safe(entry.get("content")),
                    tuple(calls),
                    _safe(entry.get("reasoning_summary")),
                    _usage_meta(entry),
                )
            )
        elif role == "tool":
            content = entry.get("content")
            items.append(
                TimelineItem(
                    "tool",
                    "Result · " + str(entry.get("tool_name") or entry.get("core_name") or "tool"),
                    _safe(_pretty_json(content) if isinstance(content, str) else content),
                    error=bool(entry.get("error")),
                )
            )
        else:
            kind = str(entry.get("kind") or "")
            label = _KIND_LABELS.get(kind, "User" if role == "user" else (role or "Entry").title())
            items.append(TimelineItem("system" if kind.startswith("engine") else "user", label, _safe(entry.get("content"))))
    return items


def _checks(record: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for item in record.get("checks") or []:
        if not isinstance(item, Mapping):
            continue
        argv = item.get("argv") or item.get("command") or []
        command = " ".join(str(part) for part in argv) if isinstance(argv, list) else str(argv)
        exit_code = item.get("exit_code")
        if item.get("timed_out"):
            status = "timed out"
        elif isinstance(item.get("ok"), bool):
            status = "passed" if item["ok"] else "failed"
        else:
            status = "passed" if exit_code == 0 else "failed" if exit_code is not None else ""
        rows.append({"command": _safe(command), "status": str(status), "exit_code": exit_code})
    return rows


def _usage_summary(record: Mapping[str, Any]) -> List[tuple[str, str]]:
    usage = record.get("usage") if isinstance(record.get("usage"), Mapping) else {}
    rows: List[tuple[str, str]] = []
    for keys, label in (
        (("prompt_tokens", "input_tokens"), "Input tokens"),
        (("completion_tokens", "output_tokens"), "Output tokens"),
        (("cache_read_tokens", "cache_read_input_tokens", "cached_tokens"), "Cache-read tokens"),
        (("reasoning_tokens",), "Reasoning tokens"),
        (("total_tokens",), "Total tokens"),
        (("requests",), "Model requests"),
    ):
        value = next((usage.get(key) for key in keys if _count(usage.get(key))), None)
        if value:
            rows.append((label, str(int(value))))
    costs = usage.get("costs")
    if isinstance(costs, Mapping) and costs:
        status = usage.get("cost_status")
        suffix = " (incomplete: some responses had no price)" if status == "unknown" else ""
        rows.append(("Cost", ", ".join(f"{value:.4f} {currency}" for currency, value in sorted(costs.items()) if isinstance(value, (int, float))) + suffix))
    misses = usage.get("prompt_cache_misses")
    if isinstance(misses, Mapping) and misses:
        rows.append(("Prompt-cache misses", ", ".join(f"{key} {value}" for key, value in sorted(misses.items()))))
    return rows


def _overview(record: Mapping[str, Any]) -> List[tuple[str, str]]:
    rows = [
        ("Session", str(record.get("session_id") or "")),
        ("Name", str(record.get("name") or "")),
        ("Repository", str(record.get("repository") or "")),
        ("Status", f"{record.get('status') or ''} · {record.get('phase') or ''}".strip(" ·")),
        ("Access", str(record.get("access_profile") or "")),
        ("Created", _when(record.get("created_at"))),
        ("Updated", _when(record.get("updated_at"))),
    ]
    return [(label, _safe(value)) for label, value in rows if value]


_CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e3e3e8;--user:#eef4ff;--model:#f6f6f8;--tool:#fbfbfd;--bad:#c62828;--ok:#2e7d32}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececf1;--muted:#9a9aa3;--line:#2c2c31;--user:#1b2433;--model:#1d1d21;--tool:#18181b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:28px 20px 60px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
.task{white-space:pre-wrap;color:var(--muted);margin:0 0 18px}table{border-collapse:collapse;width:100%;font-size:14px}
td,th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}th{color:var(--muted);font-weight:500;width:28%}
.item{border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:10px 0}.user{background:var(--user)}.assistant{background:var(--model)}
.tool{background:var(--tool)}.system{border-style:dashed}.label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.meta{float:right;font-size:12px;color:var(--muted)}.error .label{color:var(--bad)}pre{white-space:pre-wrap;word-break:break-word;margin:6px 0;font:13px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace}
.text{white-space:pre-wrap;word-break:break-word;margin:6px 0}summary{cursor:pointer;color:var(--muted);font-size:13px}.passed{color:var(--ok)}.failed{color:var(--bad)}footer{margin-top:40px;color:var(--muted);font-size:12px}
"""


def _rows(rows: Iterable[tuple[str, str]]) -> str:
    return "".join(f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>" for label, value in rows)


def render_html(record: Mapping[str, Any]) -> str:
    title = str(record.get("name") or record.get("session_id") or "KaroX session")
    parts = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">",
        f"<title>{html.escape(_safe(title))} · KaroX</title><style>{_CSS}</style></head><body><main>",
        f"<h1>{html.escape(_safe(title))}</h1>",
        f"<p class=\"task\">{html.escape(_safe(record.get('task')))}</p>",
        f"<table>{_rows(_overview(record))}</table>",
    ]
    summary = _safe(record.get("summary"))
    if summary:
        parts.append(f"<h2>Summary</h2><pre>{html.escape(summary)}</pre>")
    checks = _checks(record)
    if checks:
        parts.append("<h2>Checks</h2><table><tr><th>Command</th><th>Status</th></tr>")
        for row in checks:
            status = html.escape(row["status"])
            parts.append(f"<tr><td><code>{html.escape(row['command'])}</code></td><td class=\"{status.replace(' ', '-')}\">{status}</td></tr>")
        parts.append("</table>")
    changed = [str(item) for item in record.get("changed_files") or []]
    if changed:
        parts.append("<h2>Changed files</h2><pre>" + html.escape(_safe("\n".join(changed))) + "</pre>")
    usage = _usage_summary(record)
    if usage:
        parts.append(f"<h2>Usage</h2><table>{_rows(usage)}</table>")
    items = timeline(record)
    parts.append(f"<h2>Trajectory · {len(items)} entries</h2>")
    for item in items:
        classes = f"item {item.role}" + (" error" if item.error else "")
        meta = f"<span class=\"meta\">{html.escape(item.meta)}</span>" if item.meta else ""
        parts.append(f"<section class=\"{classes}\">{meta}<div class=\"label\">{html.escape(item.title)}</div>")
        if item.reasoning:
            parts.append(f"<details><summary>reasoning summary</summary><pre>{html.escape(item.reasoning)}</pre></details>")
        if item.text:
            if item.role == "tool" and len(item.text) > 600:
                preview = " ".join(item.text.split())[:140]
                parts.append(
                    f"<details><summary>{html.escape(preview)} … ({len(item.text)} characters)</summary>"
                    f"<pre>{html.escape(item.text)}</pre></details>"
                )
            elif item.role in {"assistant", "user"}:
                parts.append(f"<div class=\"text\">{html.escape(item.text)}</div>")
            else:
                parts.append(f"<pre>{html.escape(item.text)}</pre>")
        for name, arguments in item.calls:
            parts.append(f"<details><summary>call · {html.escape(name)}</summary><pre>{html.escape(arguments)}</pre></details>")
        parts.append("</section>")
    parts.append("<footer>Exported by KaroX. Secrets are redacted; long outputs are shortened.</footer></main></body></html>")
    return "".join(parts)


def render_markdown(record: Mapping[str, Any]) -> str:
    title = _safe(record.get("name") or record.get("session_id") or "KaroX session")
    fence = "````"
    lines = [f"# {title}", "", _safe(record.get("task")), ""]
    lines.extend(f"- **{label}:** {value}" for label, value in _overview(record))
    summary = _safe(record.get("summary"))
    if summary:
        lines += ["", "## Summary", "", summary]
    checks = _checks(record)
    if checks:
        lines += ["", "## Checks", ""]
        lines.extend(f"- `{row['command']}` — {row['status']}" for row in checks)
    changed = [str(item) for item in record.get("changed_files") or []]
    if changed:
        lines += ["", "## Changed files", ""]
        lines.extend(f"- `{_safe(item)}`" for item in changed)
    usage = _usage_summary(record)
    if usage:
        lines += ["", "## Usage", ""]
        lines.extend(f"- {label}: {value}" for label, value in usage)
    lines += ["", "## Trajectory", ""]
    for item in timeline(record):
        heading = f"### {item.title}" + (f" · {item.meta}" if item.meta else "") + (" · error" if item.error else "")
        lines += [heading, ""]
        if item.reasoning:
            lines += ["<details><summary>reasoning summary</summary>", "", fence, item.reasoning, fence, "", "</details>", ""]
        if item.text:
            lines += [fence, item.text, fence, ""] if item.role != "assistant" else [item.text, ""]
        for name, arguments in item.calls:
            lines += [f"Call `{name}`:", "", fence + "json", arguments, fence, ""]
    return "\n".join(lines).rstrip() + "\n"


def export_session(record: Any, format: str = "html") -> str:
    value = record.to_dict() if hasattr(record, "to_dict") else dict(record)
    if format == "html":
        return render_html(value)
    if format in {"md", "markdown"}:
        return render_markdown(value)
    if format == "json":
        return json.dumps(redact(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    raise ValueError(f"unsupported export format: {format}")


__all__ = ["TimelineItem", "export_session", "render_html", "render_markdown", "timeline"]
