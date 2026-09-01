"""Data-only user commands for the KaroX TUI.

A user command is a named prompt template, never executable code.  Expanding one
therefore stays on the ordinary AgentKernel path and cannot bypass KaroX Core,
capability policy, RiskEngine, verification, or provider credential handling.

Entries live in the local KaroX config directory and can be user-wide or scoped
to one repository fingerprint. Secret-shaped templates are refused rather than
silently persisted with redactions.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from .models import repository_fingerprint
from .paths import config_dir
from .security import contains_credential
from .sessions import _exclusive_file_lock

_SCHEMA_VERSION = 1
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_MAX_TEMPLATE_CHARS = 8000


class UserCommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class UserCommand:
    name: str
    template: str
    scope: str = "user"
    repository_fingerprint: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _SAFE_NAME.fullmatch(self.name) is None:
            raise ValueError("command name must be 1-32 lowercase letters/digits/_/- and start with a letter")
        if not isinstance(self.template, str) or not self.template.strip():
            raise ValueError("command template must be non-empty text")
        if len(self.template) > _MAX_TEMPLATE_CHARS or "\x00" in self.template:
            raise ValueError(f"command template must be at most {_MAX_TEMPLATE_CHARS} safe characters")
        if self.scope not in {"user", "repository", "project"}:
            raise ValueError("command scope must be user, repository, or project")
        if self.scope in {"repository", "project"} and not self.repository_fingerprint:
            raise ValueError("repository/project-scoped command requires a repository fingerprint")
        if self.scope == "user" and self.repository_fingerprint is not None:
            raise ValueError("user-scoped command cannot carry a repository fingerprint")
        if contains_credential(self.template):
            raise ValueError("command templates cannot contain credential-shaped content")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def expand(self, arguments: str) -> str:
        arguments = str(arguments).strip()
        if "{{args}}" in self.template:
            return self.template.replace("{{args}}", arguments)
        return self.template if not arguments else f"{self.template}\n\nUser arguments:\n{arguments}"


class UserCommandStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = (path or (config_dir() / "vnext" / "user-commands.json")).expanduser().resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def _load(self) -> list[UserCommand]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UserCommandError(f"cannot read user commands: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
            raise UserCommandError("user command registry has unsupported schema")
        rows = raw.get("commands")
        if not isinstance(rows, list):
            raise UserCommandError("user command registry commands must be a list")
        result: list[UserCommand] = []
        for row in rows:
            if not isinstance(row, dict):
                raise UserCommandError("user command entry must be an object")
            try:
                result.append(UserCommand(**row))
            except (TypeError, ValueError) as exc:
                raise UserCommandError(f"invalid user command entry: {exc}") from exc
        return result

    def _save(self, rows: list[UserCommand]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temp = Path(raw)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {"schema_version": _SCHEMA_VERSION, "commands": [row.to_dict() for row in rows]},
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _project_fingerprint(repository: Path) -> str:
        return repository_fingerprint(repository.expanduser().resolve(strict=True))

    def _repository_commands(self, repository: Path) -> tuple[UserCommand, ...]:
        root = repository.expanduser().resolve(strict=True)
        directory = root / ".karox" / "commands"
        try:
            if not directory.exists():
                return ()
            if directory.is_symlink() or not directory.is_dir():
                return ()
            resolved = directory.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            return ()
        fingerprint = self._project_fingerprint(root)
        rows: list[UserCommand] = []
        try:
            candidates = sorted(resolved.glob("*.md"))[:100]
        except OSError:
            return ()
        for path in candidates:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                name = path.stem.casefold()
                if _SAFE_NAME.fullmatch(name) is None or path.stat().st_size > _MAX_TEMPLATE_CHARS * 4:
                    continue
                text = path.read_text(encoding="utf-8", errors="strict").strip()
                if not text or len(text) > _MAX_TEMPLATE_CHARS or contains_credential(text):
                    continue
                rows.append(
                    UserCommand(
                        name=name,
                        template=text,
                        scope="repository",
                        repository_fingerprint=fingerprint,
                    )
                )
            except (OSError, UnicodeDecodeError, ValueError):
                continue
        return tuple(rows)

    def list(self, *, repository: Optional[Path] = None) -> tuple[UserCommand, ...]:
        rows = self._load()
        fingerprint = self._project_fingerprint(repository) if repository is not None else None
        visible = [
            row
            for row in rows
            if row.scope == "user"
            or (fingerprint is not None and row.repository_fingerprint == fingerprint)
        ]
        if repository is not None:
            visible.extend(self._repository_commands(repository))
        # Precedence is deliberate: global user defaults < repository-authored
        # data-only commands < explicit local project overrides. No repository
        # command can override a built-in TUI command; that guard lives at the
        # routing/catalog boundary as well.
        rank = {"user": 0, "repository": 1, "project": 2}
        by_name: dict[str, UserCommand] = {}
        for row in sorted(visible, key=lambda item: (item.name, rank[item.scope])):
            by_name[row.name] = row
        return tuple(by_name[name] for name in sorted(by_name))

    def get(self, name: str, *, repository: Optional[Path] = None) -> UserCommand:
        validated = UserCommand(name=name, template="placeholder").name
        for row in self.list(repository=repository):
            if row.name == validated:
                return row
        raise UserCommandError(f"user command does not exist: /{validated}")

    def put(self, name: str, template: str, *, scope: str = "user", repository: Optional[Path] = None) -> UserCommand:
        fingerprint = None
        if scope == "project":
            if repository is None:
                raise ValueError("project-scoped command requires a repository")
            fingerprint = self._project_fingerprint(repository)
        row = UserCommand(name=name, template=template, scope=scope, repository_fingerprint=fingerprint)
        with _exclusive_file_lock(self.lock_path):
            rows = self._load()
            rows = [
                item
                for item in rows
                if not (
                    item.name == row.name
                    and item.scope == row.scope
                    and item.repository_fingerprint == row.repository_fingerprint
                )
            ]
            rows.append(row)
            self._save(rows)
        return row

    def remove(self, name: str, *, scope: str = "user", repository: Optional[Path] = None) -> UserCommand:
        fingerprint = None
        if scope == "project":
            if repository is None:
                raise ValueError("project-scoped command requires a repository")
            fingerprint = self._project_fingerprint(repository)
        with _exclusive_file_lock(self.lock_path):
            rows = self._load()
            for index, row in enumerate(rows):
                if row.name == name and row.scope == scope and row.repository_fingerprint == fingerprint:
                    removed = rows.pop(index)
                    self._save(rows)
                    return removed
        raise UserCommandError(f"user command does not exist: /{name}")


__all__ = ["UserCommand", "UserCommandError", "UserCommandStore"]
