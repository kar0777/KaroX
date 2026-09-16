"""``karox quickstart`` -- the first minute without documentation.

A new user has three questions: is this a repository KaroX can work in, is any
model or hosted client already connected, and what is the one command to run
next. This module answers exactly those and nothing else. It reads existing
stores; it never writes, never prompts, never launches a bridge, and never
prints a secret. The words "bridge", "tunnel", "OAuth" and "profile" do not
appear on the first screen unless something is already configured that uses
them.

The report is bilingual by data, not by branching in the renderer: every
human-facing string is looked up through :func:`_text` so a missing translation
is a test failure, not a mixed-language screen.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

# Language codes are the same two the TUI persists; anything else falls back to
# English so a hand-edited ui.json cannot produce an untranslated screen.
_LANGUAGES = ("en", "ru")

_TEXT: Mapping[str, Mapping[str, str]] = {
    "title": {
        "en": "KaroX quickstart",
        "ru": "KaroX: быстрый старт",
    },
    "repository": {"en": "Repository", "ru": "Репозиторий"},
    "repository_missing": {
        "en": "not a Git repository -- run `karox` inside a Git checkout",
        "ru": "не Git-репозиторий -- запусти `karox` внутри Git-checkout",
    },
    "language": {"en": "Language", "ru": "Язык"},
    "language_unset": {
        "en": "not chosen yet (the first `karox` run asks)",
        "ru": "ещё не выбран (первый запуск `karox` спросит)",
    },
    "providers": {"en": "API models", "ru": "API-модели"},
    "providers_none": {"en": "none connected", "ru": "не подключены"},
    "selected": {"en": "selected", "ru": "выбрана"},
    "clients": {"en": "Hosted clients", "ru": "Внешние клиенты"},
    "clients_none": {
        "en": "none connected (ChatGPT, Claude and other MCP clients connect through /connect)",
        "ru": "не подключены (ChatGPT, Claude и другие MCP-клиенты подключаются через /connect)",
    },
    "sessions": {"en": "Sessions in this repository", "ru": "Сессии в этом репозитории"},
    "next": {"en": "Next", "ru": "Дальше"},
    "next_repository": {
        "en": "cd into a Git repository, then run `karox`",
        "ru": "перейди в Git-репозиторий и запусти `karox`",
    },
    "next_connect": {
        "en": "run `karox`, then `/connect` to add an API key or connect ChatGPT/Claude",
        "ru": "запусти `karox`, затем `/connect`, чтобы добавить API-ключ или подключить ChatGPT/Claude",
    },
    "next_task": {
        "en": "run `karox` and type your first task; start in Observe, switch to Build when ready",
        "ru": "запусти `karox` и напиши первую задачу; начни с Observe, переключись на Build когда готов",
    },
    "next_resume": {
        "en": "run `karox` -- the previous session in this repository resumes without replaying finished work",
        "ru": "запусти `karox` -- прошлая сессия в этом репозитории продолжится без повтора выполненного",
    },
    "next_bridge_start": {
        "en": "saved bridge `{name}` is configured but not running -- run `karox bridge start --saved {name}` (it relaunches detached and keeps the same public URL)",
        "ru": "сохранённый bridge `{name}` настроен, но не запущен -- запусти `karox bridge start --saved {name}` (он поднимется как detached owner и сохранит публичный URL)",
    },
    "sandbox_note": {
        "en": "KaroX is not an OS sandbox: an approved process runs with your user rights.",
        "ru": "KaroX не системная песочница: разрешённый процесс выполняется с правами твоего пользователя.",
    },
}


def _text(key: str, language: str) -> str:
    entry = _TEXT[key]
    return entry.get(language) or entry["en"]


def normalize_language(value: Optional[str]) -> str:
    return value if value in _LANGUAGES else "en"


@dataclass(frozen=True)
class QuickstartReport:
    """Everything the first screen shows, as data first and prose second."""

    repository: Optional[str]
    repository_is_git: bool
    language: Optional[str]
    providers: tuple[str, ...]
    selected_model: Optional[str]
    clients: tuple[str, ...]
    session_count: int
    next_step: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "repository_is_git": self.repository_is_git,
            "language": self.language,
            "providers": list(self.providers),
            "selected_model": self.selected_model,
            "clients": list(self.clients),
            "session_count": self.session_count,
            "next_step": self.next_step,
            "notes": list(self.notes),
        }


def git_toplevel(path: Path, *, run: Callable[..., Any] = subprocess.run) -> Optional[Path]:
    """Return the Git work-tree root containing ``path``, or None.

    ``git`` is asked rather than walking for ``.git`` by hand so that worktrees
    and ``gitdir:`` files resolve the same way the rest of KaroX resolves them.
    A missing ``git`` binary is reported as "not a repository", which is the
    truthful answer for a runtime that needs Git to do anything.
    """
    try:
        result = run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    top = str(getattr(result, "stdout", "") or "").strip()
    return Path(top) if top else None


def _same_path(left: str, right: Path) -> bool:
    try:
        candidate = Path(left).expanduser().resolve(strict=False)
    except OSError:
        return False
    return os.path.normcase(str(candidate)) == os.path.normcase(str(right))


def build_report(
    cwd: Path,
    *,
    language: Optional[str],
    provider_ids: Sequence[str],
    selected_model: Optional[str],
    client_labels: Sequence[str],
    session_repositories: Sequence[str],
    down_bridges: Sequence[str] = (),
    toplevel: Optional[Callable[[Path], Optional[Path]]] = None,
) -> QuickstartReport:
    """Pure assembly: every input is a plain value so the decision is testable.

    ``toplevel`` is resolved at call time rather than bound as a default so a
    test (or a caller) that patches :func:`git_toplevel` on the module sees the
    patch take effect.
    """
    root = (toplevel or git_toplevel)(cwd)
    repository = str(root) if root is not None else str(cwd)
    is_git = root is not None
    sessions_here = (
        sum(1 for item in session_repositories if root is not None and _same_path(item, root))
        if is_git
        else 0
    )
    connected = bool(provider_ids) or bool(client_labels)

    if not is_git:
        next_key = "next_repository"
    elif down_bridges:
        # One configured client is down: repairing it is the only next action
        # worth a slot on the first screen. Everything else can wait.
        lang_probe = normalize_language(language)
        return QuickstartReport(
            repository=repository,
            repository_is_git=is_git,
            language=language if language in _LANGUAGES else None,
            providers=tuple(provider_ids),
            selected_model=selected_model,
            clients=tuple(client_labels),
            session_count=sessions_here,
            next_step=_text("next_bridge_start", lang_probe).format(name=down_bridges[0]),
            notes=(_text("sandbox_note", lang_probe),),
        )
    elif not connected:
        next_key = "next_connect"
    elif sessions_here:
        next_key = "next_resume"
    else:
        next_key = "next_task"

    lang = normalize_language(language)
    return QuickstartReport(
        repository=repository,
        repository_is_git=is_git,
        language=language if language in _LANGUAGES else None,
        providers=tuple(provider_ids),
        selected_model=selected_model,
        clients=tuple(client_labels),
        session_count=sessions_here,
        next_step=_text(next_key, lang),
        notes=(_text("sandbox_note", lang),),
    )


def render_report(report: QuickstartReport, language: Optional[str]) -> str:
    """Six short lines. Nothing here is a log; every line is a decision input."""
    lang = normalize_language(language)
    lines = [_text("title", lang), ""]

    repository_value = (
        report.repository if report.repository_is_git else _text("repository_missing", lang)
    )
    lines.append(f"{_text('repository', lang)}: {repository_value}")

    language_value = report.language or _text("language_unset", lang)
    lines.append(f"{_text('language', lang)}: {language_value}")

    if report.providers:
        providers = ", ".join(report.providers)
        if report.selected_model:
            providers += f" ({_text('selected', lang)}: {report.selected_model})"
    else:
        providers = _text("providers_none", lang)
    lines.append(f"{_text('providers', lang)}: {providers}")

    clients = ", ".join(report.clients) if report.clients else _text("clients_none", lang)
    lines.append(f"{_text('clients', lang)}: {clients}")

    if report.repository_is_git:
        lines.append(f"{_text('sessions', lang)}: {report.session_count}")

    lines.append("")
    lines.append(f"{_text('next', lang)}: {report.next_step}")
    for note in report.notes:
        lines.append(note)
    return "\n".join(lines) + "\n"


def _load_language_from_preferences(config_root: Path) -> Optional[str]:
    """Read the persisted UI language without importing the heavyweight TUI.

    ``quickstart`` is intentionally the first command a new user runs. Importing
    :mod:`karox.tui` just to read one JSON key pulled Textual, provider and bridge
    modules into the cold path and dominated startup latency. The preferences
    file is a tiny stable contract, so read only that file here and fail soft in
    the same way the TUI loader does.
    """

    try:
        payload = json.loads((config_root / "vnext" / "ui.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("language")
    return value if value in _LANGUAGES else None


def collect_report(cwd: Optional[Path] = None) -> QuickstartReport:
    """Gather inputs from the real stores; each store failure degrades to "none".

    A quickstart that crashes because one JSON file is malformed would be the
    opposite of its purpose, so every read is guarded and the screen still
    renders with whatever could be read.
    """
    from .paths import config_dir, session_dir
    from .registry import ProviderRegistry
    from .sessions import SessionStore
    from .web_bridge_profiles import WebBridgeProfileStore

    base = (cwd or Path.cwd()).resolve(strict=False)

    language: Optional[str]
    try:
        language = _load_language_from_preferences(config_dir())
    except Exception:
        language = None

    provider_ids: list[str] = []
    selected: Optional[str] = None
    try:
        registry = ProviderRegistry(config_dir() / "vnext" / "providers.json")
        provider_ids = [item.provider_id for item in registry.providers()]
        model = registry.selected_model()
        if model is not None:
            selected = f"{model.provider_id}/{model.model_id}"
    except Exception:
        pass

    clients: list[str] = []
    try:
        for profile in WebBridgeProfileStore().list():
            clients.append(f"{profile.target_profile} ({profile.name})")
    except Exception:
        pass

    session_repositories: list[str] = []
    try:
        for record in SessionStore(session_dir()).list(include_archived=False):
            if not getattr(record, "revoked", False):
                session_repositories.append(str(record.repository))
    except Exception:
        pass

    down_bridges: list[str] = []
    try:
        from .saved_bridge_supervisor import saved_bridge_supervisor_status

        for profile_name in [label.split(" (", 1)[1].rstrip(")") for label in clients]:
            try:
                state = saved_bridge_supervisor_status(profile_name)
            except Exception:
                continue
            if state.get("desired_running") and not (
                state.get("supervisor_alive") or state.get("bridge_pid")
            ):
                down_bridges.append(profile_name)
    except Exception:
        pass

    return build_report(
        base,
        language=language,
        provider_ids=provider_ids,
        selected_model=selected,
        client_labels=clients,
        session_repositories=session_repositories,
        down_bridges=down_bridges,
    )


def _write_stdout(text: str) -> None:
    """Write human output without letting a legacy Windows code page crash UX."""

    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(encoding, "replace").decode(encoding, "replace"))


def cli_main(argv: Optional[Sequence[str]] = None) -> int:
    """Lightweight console path used before importing the full legacy CLI."""

    parser = argparse.ArgumentParser(
        prog="karox quickstart",
        description="Show what is connected and the one command to run next.",
    )
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path("."),
        help="directory to inspect; defaults to the current directory",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv or ()))
    report = collect_report(args.repository.expanduser())
    if args.json:
        _write_stdout(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        )
    else:
        _write_stdout(render_report(report, report.language).rstrip("\n"))
    return 0


__all__ = [
    "QuickstartReport",
    "build_report",
    "cli_main",
    "collect_report",
    "git_toplevel",
    "normalize_language",
    "render_report",
]
