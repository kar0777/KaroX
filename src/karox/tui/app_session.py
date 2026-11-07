"""TUI session bootstrap, background work and welcome/palette."""

from __future__ import annotations

from typing import Any, Callable
from . import commands as slash
from .i18n import tr
from .protocol import SessionState
from .views import (
    same_repository,
)
from .widgets import (
    MenuItem,
    WelcomeCard,
    welcome_text,
)

from .app_support import (  # noqa: F401
    EFFORT_LEVELS as EFFORT_LEVELS,
    MODES as MODES,
    LANGUAGES as LANGUAGES,
    _REFUSED_WHILE_RUNNING as _REFUSED_WHILE_RUNNING,
    RunState as RunState,
    _display_path as _display_path,
    _cost_text as _cost_text,
    _save_preference as _save_preference,
    _git_branch as _git_branch,
    _detect_verification as _detect_verification,
)


class AppSessionMixin:
    """Methods composed into KaroxApp; see app.py."""

    def _load_startup(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
        record = self.backend.session(self.session_id) if self._resume_requested else None
        try:
            model = self.backend.selected_model()
        except Exception:  # noqa: BLE001 - "no model" is a state, not a crash
            model = None
        return record, model, _git_branch(self.repository)

    def _startup_loaded(
        self, loaded: tuple[dict[str, Any] | None, dict[str, Any] | None, str]
    ) -> None:
        record, model, branch = loaded
        self._model = model
        self._model_loaded = True
        self._branch = branch
        if record is not None:
            repository = record.get("repository")
            if isinstance(repository, str) and repository and not same_repository(
                repository, self.repository
            ):
                self.session_id = self._new_session_id()
                self._continue = False
                self._notice(
                    tr(self.language, "notice.session_repo", repository=repository), "warning"
                )
            else:
                self._continue = True
                self.checkpoint = SessionState.from_record(record)
        self._refresh_welcome()
        self._refresh_chrome()
        self._refresh_status()

    def _background(
        self,
        work: Callable[[], Any],
        done: Callable[[Any], None],
        *,
        activity: str = "",
        failed: Callable[[str], None] | None = None,
    ) -> None:
        """Run one CLI call in a thread and hand its result back to the screen."""

        if activity:
            self._lookup_started(activity)

        def runner() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - every failure becomes a notice
                message = str(exc).strip() or type(exc).__name__
                self._from_thread(self._background_failed, bool(activity), failed, message)
                return
            self._from_thread(self._background_done, bool(activity), done, result)

        self.run_worker(runner, thread=True, group="lookup", exit_on_error=False)

    def _from_thread(self, callback: Callable[..., Any], *arguments: Any) -> None:
        try:
            self.call_from_thread(callback, *arguments)
        except RuntimeError:
            # The app is shutting down; there is no screen left to update.
            pass

    def _background_done(self, had_activity: bool, done: Callable[[Any], None], result: Any) -> None:
        if had_activity:
            self._lookup_finished()
        done(result)

    def _background_failed(
        self, had_activity: bool, failed: Callable[[str], None] | None, message: str
    ) -> None:
        if had_activity:
            self._lookup_finished()
        if failed is not None:
            failed(message)
        else:
            self._notice(message, "error")

    def _lookup_started(self, text: str) -> None:
        # A slow CLI lookup (doctor, an MCP probe, the model list) gets the
        # same dots as the agent, so the screen never looks frozen.
        self._lookups += 1
        self._lookup_text = text
        self._refresh_working()

    def _lookup_finished(self) -> None:
        self._lookups = max(0, self._lookups - 1)
        if not self._lookups:
            self._lookup_text = ""
        self._refresh_working()

    def _add_welcome(self) -> None:
        self._welcome_card = self.chat.add(
            WelcomeCard(
                tr(self.language, "welcome.title"),
                tr(self.language, "welcome.body"),
                facts=self._welcome_facts(),
            )
        )

    def _refresh_welcome(self) -> None:
        card = self._welcome_card
        if card is None or not card.is_attached:
            return
        card.update(
            welcome_text(
                tr(self.language, "welcome.title"),
                tr(self.language, "welcome.body"),
                self._welcome_facts(),
            )
        )

    def _welcome_facts(self) -> list[str]:
        lang = self.language
        path = _display_path(self.repository)
        if self._branch:
            path = f"{path} ({self._branch})"
        facts = [tr(lang, "welcome.cwd", path=path)]
        model = self._model_label() or tr(
            lang, "status.no_model" if self._model_loaded else "status.loading"
        )
        facts.append(tr(lang, "welcome.model", model=model))
        if self._verification:
            commands = "; ".join(" ".join(item) for item in self._verification)
            facts.append(tr(self.language, "welcome.facts.verification", commands=commands))
        elif self._detected_verification:
            commands = "; ".join(" ".join(item) for item in self._detected_verification)
            facts.append(tr(self.language, "welcome.facts.suggested", commands=commands))
        return facts

    def _show_palette(self, query: str) -> None:
        items = [
            MenuItem(
                command.name,
                f"/{command.name}" + (f" {command.argument}" if command.argument else ""),
                tr(self.language, command.description),
                search=" ".join(command.aliases),
            )
            for command in slash.match(query)
        ]
        self._menu_context = {}
        self.menu.open(
            "commands",
            "",
            items,
            hint=tr(self.language, "menu.palette_hint"),
            empty=tr(self.language, "menu.empty"),
            filtered=False,
        )

    def _open_picker(
        self,
        kind: str,
        title: str,
        items: list[MenuItem],
        *,
        current: str | None = None,
        query: str = "",
        context: dict[str, Any] | None = None,
        hint: str | None = None,
    ) -> None:
        self._menu_context = dict(context or {})
        self.menu.open(
            kind,
            title,
            items,
            hint=hint or tr(self.language, "menu.picker_hint"),
            empty=tr(self.language, "menu.empty"),
            current=current,
            query=query,
        )
        composer = self.composer
        if str(composer.text) != query:
            composer.set_text(query)
        if composer.display:
            composer.focus()
