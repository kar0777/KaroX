"""Small workspace picker for the human-facing KaroX TUI.

The picker owns presentation only.  Repository safety policy and application
state changes stay in :class:`karox.tui.KaroXApp`, so the slash command and the
UI cannot disagree about what a valid workspace is.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .project_registry import ProjectRegistry

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option


class WorkspacePickerScreen(ModalScreen[Optional[str]]):
    """Choose a repository by typing a path or selecting a recent one."""

    BINDINGS = [
        Binding("escape", "cancel", "Back", priority=True),
    ]
    DEFAULT_CSS = """
    WorkspacePickerScreen { align: center middle; background: #0e0c08 92%; }
    #workspace-dialog { width: 100%; max-width: 82; height: auto; max-height: 88%;
      background: #1a1712; border: round #c6a56b; padding: 1 2; }
    #workspace-title { height: 1; text-style: bold; color: #e5e5e5; }
    #workspace-current { height: auto; color: #8a7e6a; margin-bottom: 1; }
    #workspace-path { border: round #4a4338; background: #20201c; color: #e5e5e5; }
    #workspace-recent-label { height: 1; color: #c6bca8; margin-top: 1; }
    #workspace-recent { height: auto; max-height: 10; border: none;
      background: #1a1712; }
    #workspace-error { min-height: 0; color: #e0a3a3; text-wrap: wrap; }
    #workspace-buttons { height: 3; align-horizontal: right; margin-top: 1; }
    #workspace-buttons Button { margin-left: 1; }
    """

    def __init__(
        self,
        current: Path,
        recent: Sequence[str] = (),
        language: str = "ru",
    ) -> None:
        super().__init__()
        self.current = current.expanduser().resolve()
        self.recent = self._normalise_recent(recent)
        self.language = language
        self._recent_by_id = {
            f"workspace-recent-{index}": value
            for index, value in enumerate(self.recent)
        }

    def _label(self, russian: str, english: str) -> str:
        return english if self.language != "ru" else russian

    def _normalise_recent(self, values: Sequence[str]) -> tuple[str, ...]:
        current_key = str(self.current).casefold()
        result: list[str] = []
        seen: set[str] = set()
        for raw in values:
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                value = str(Path(raw).expanduser().resolve())
            except (OSError, RuntimeError):
                continue
            key = value.casefold()
            if key == current_key or key in seen or not Path(value).is_dir():
                continue
            seen.add(key)
            result.append(value)
        return tuple(result)

    def compose(self) -> ComposeResult:
        with Vertical(id="workspace-dialog"):
            yield Static(
                self._label("Рабочая папка", "Workspace"),
                id="workspace-title",
                markup=False,
            )
            yield Static(
                self._label("Сейчас: ", "Current: ") + str(self.current),
                id="workspace-current",
                markup=False,
            )
            yield Input(
                placeholder=self._label(
                    r"D:\путь\к\проекту",
                    r"D:\path\to\project",
                ),
                id="workspace-path",
            )
            yield Static(
                self._label("Недавние", "Recent"),
                id="workspace-recent-label",
                markup=False,
            )
            options = [
                Option(value, id=identifier)
                for identifier, value in self._recent_by_id.items()
            ]
            yield OptionList(*options, id="workspace-recent")
            yield Static("", id="workspace-error", markup=False)
            with Horizontal(id="workspace-buttons"):
                yield Button(
                    self._label("Открыть", "Open"),
                    id="workspace-open",
                    variant="primary",
                )
                yield Button(
                    self._label("Назад", "Back"),
                    id="workspace-cancel",
                )

    def on_mount(self) -> None:
        recent = self.query_one("#workspace-recent", OptionList)
        recent.display = bool(self.recent)
        self.query_one("#workspace-recent-label", Static).display = bool(self.recent)
        self.query_one("#workspace-path", Input).focus()

    def _set_error(self, message: str) -> None:
        self.query_one("#workspace-error", Static).update(message)

    def _submit(self, value: str) -> None:
        raw = value.strip().strip('"')
        if not raw:
            self._set_error(
                self._label("Введите путь к папке проекта.", "Enter a project folder.")
            )
            return
        candidate = Path(raw).expanduser()
        if not candidate.is_dir():
            self._set_error(
                self._label("Такой папки не существует.", "That folder does not exist.")
            )
            return
        self.dismiss(str(candidate.resolve()))

    @on(Input.Submitted, "#workspace-path")
    def _path_submitted(self, event: Input.Submitted) -> None:
        self._submit(event.value)

    @on(OptionList.OptionSelected, "#workspace-recent")
    def _recent_selected(self, event: OptionList.OptionSelected) -> None:
        value = self._recent_by_id.get(str(getattr(event.option, "id", "")))
        if value:
            self.dismiss(value)

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "workspace-cancel":
            self.dismiss(None)
            return
        if event.button.id == "workspace-open":
            self._submit(self.query_one("#workspace-path", Input).value)

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass(frozen=True)
class WorkspaceAction:
    action: str
    project_id: Optional[str] = None
    path: Optional[str] = None


class WorkspaceManagerScreen(ModalScreen[Optional[WorkspaceAction]]):
    """Compact multi-project manager opened by Ctrl+W."""

    BINDINGS = [Binding("escape", "cancel", "Back", priority=True)]
    DEFAULT_CSS = """
    WorkspaceManagerScreen { align: center middle; background: #0e0c08 92%; }
    #workspace-manager { width: 100%; max-width: 92; height: auto; max-height: 90%;
      background: #1a1712; border: round #c6a56b; padding: 1 2; }
    #workspace-manager-title { height: 1; text-style: bold; color: #e5e5e5; }
    #workspace-manager-hint { height: auto; color: #8a7e6a; margin-bottom: 1; }
    #workspace-projects { height: auto; max-height: 14; border: round #3a342c;
      background: #171510; margin-bottom: 1; }
    #workspace-add-path { border: round #4a4338; background: #20201c; color: #e5e5e5; }
    #workspace-manager-error { min-height: 0; color: #e0a3a3; text-wrap: wrap; }
    #workspace-manager-buttons { height: 3; align-horizontal: right; margin-top: 1; }
    #workspace-manager-buttons Button { margin-left: 1; }
    """

    def __init__(
        self,
        registry: ProjectRegistry,
        current: Path,
        language: str = "ru",
    ) -> None:
        super().__init__()
        self.registry = registry
        self.current = current.expanduser().resolve()
        self.language = language
        current_entry = registry.entry_for_path(self.current)
        self._selected_project_id = (
            current_entry.project_id
            if current_entry is not None
            else registry.default_project_id
        )
        self._project_by_option_id = {
            f"workspace-project-{index}": entry.project_id
            for index, entry in enumerate(registry.projects)
        }

    def _label(self, russian: str, english: str) -> str:
        return english if self.language != "ru" else russian

    def compose(self) -> ComposeResult:
        with Vertical(id="workspace-manager"):
            yield Static(
                self._label("Рабочие папки", "Workspaces"),
                id="workspace-manager-title",
                markup=False,
            )
            yield Static(
                self._label(
                    "Enter/Use — выбрать для новой задачи • Default — сделать основной",
                    "Enter/Use — select for a new task • Default — make primary",
                ),
                id="workspace-manager-hint",
                markup=False,
            )
            options = []
            for option_id, project_id in self._project_by_option_id.items():
                entry = self.registry.get(project_id)
                markers = []
                if entry.project_id == self.registry.default_project_id:
                    markers.append("★")
                if entry.path.casefold() == str(self.current).casefold():
                    markers.append("●")
                prefix = "".join(markers) + (" " if markers else "")
                options.append(
                    Option(
                        f"{prefix}{entry.label}  —  {entry.path}",
                        id=option_id,
                    )
                )
            yield OptionList(*options, id="workspace-projects")
            yield Input(
                placeholder=self._label(
                    r"Добавить папку: D:\путь\к\проекту  (Enter)",
                    r"Add folder: D:\path\to\project  (Enter)",
                ),
                id="workspace-add-path",
            )
            yield Static("", id="workspace-manager-error", markup=False)
            with Horizontal(id="workspace-manager-buttons"):
                yield Button(self._label("Использовать", "Use"), id="workspace-use", variant="primary")
                yield Button(self._label("По умолчанию", "Default"), id="workspace-default")
                yield Button(self._label("Удалить", "Remove"), id="workspace-remove", variant="error")
                yield Button(self._label("Назад", "Back"), id="workspace-back")

    def on_mount(self) -> None:
        options = self.query_one("#workspace-projects", OptionList)
        selected_index = 0
        if self._selected_project_id is not None:
            for index, project_id in enumerate(self._project_by_option_id.values()):
                if project_id == self._selected_project_id:
                    selected_index = index
                    break
        if self.registry.projects:
            options.highlighted = selected_index
            options.focus()

    def _selected(self) -> Optional[str]:
        options = self.query_one("#workspace-projects", OptionList)
        index = options.highlighted
        if index is None or index < 0 or index >= len(self._project_by_option_id):
            return self._selected_project_id
        return tuple(self._project_by_option_id.values())[index]

    def _set_error(self, message: str) -> None:
        self.query_one("#workspace-manager-error", Static).update(message)

    def _submit_add(self, raw: str) -> None:
        value = raw.strip().strip('"')
        if not value:
            self._set_error(self._label("Введите путь к папке.", "Enter a folder path."))
            return
        candidate = Path(value).expanduser()
        if not candidate.is_dir():
            self._set_error(self._label("Такой папки не существует.", "That folder does not exist."))
            return
        self.dismiss(WorkspaceAction("add", path=str(candidate.resolve())))

    @on(Input.Submitted, "#workspace-add-path")
    def _add_submitted(self, event: Input.Submitted) -> None:
        self._submit_add(event.value)

    @on(OptionList.OptionHighlighted, "#workspace-projects")
    def _project_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        project_id = self._project_by_option_id.get(str(getattr(event.option, "id", "")))
        if project_id:
            self._selected_project_id = project_id

    @on(OptionList.OptionSelected, "#workspace-projects")
    def _project_selected(self, event: OptionList.OptionSelected) -> None:
        project_id = self._project_by_option_id.get(str(getattr(event.option, "id", "")))
        if project_id:
            self.dismiss(WorkspaceAction("use", project_id=project_id))

    @on(Button.Pressed)
    def _manager_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "workspace-back":
            self.dismiss(None)
            return
        project_id = self._selected()
        if project_id is None:
            self._set_error(self._label("Нет выбранной папки.", "No workspace is selected."))
            return
        if event.button.id == "workspace-use":
            self.dismiss(WorkspaceAction("use", project_id=project_id))
        elif event.button.id == "workspace-default":
            self.dismiss(WorkspaceAction("default", project_id=project_id))
        elif event.button.id == "workspace-remove":
            self.dismiss(WorkspaceAction("remove", project_id=project_id))

    def action_cancel(self) -> None:
        self.dismiss(None)
