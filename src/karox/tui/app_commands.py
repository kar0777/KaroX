"""TUI slash commands, provider connect, sessions, skills and MCP."""

from __future__ import annotations

import os
import shlex
from typing import Any
from rich.text import Text
from . import commands as slash
from .i18n import normalise_language, tr
from .modals import McpAddScreen, ProviderSetupScreen
from .protocol import SessionState
from .views import (
    ADD_SERVER_KEY,
    CUSTOM_PROVIDER_KEY,
    catalog_items,
    doctor_text,
    help_text,
    lines_text,
    mcp_items,
    model_items,
    same_repository,
    session_items,
    session_state_lines,
    skill_text,
    status_text,
)
from .widgets import (
    MUTED,
    MenuItem,
    UserMessage,
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


class AppCommandsMixin:
    """Methods composed into KaroxApp; see app.py."""

    def _command(self, value: str) -> None:
        head, _, rest = value[1:].partition(" ")
        command = slash.resolve(head)
        if command is None:
            self.chat.add(UserMessage(value, command=True))
            self._notice(
                tr(self.language, "notice.unknown_command", command=head.strip() or "?"),
                "warning",
            )
            return
        self._run_command(command, rest.strip())

    def _run_command(self, command: slash.SlashCommand, argument: str) -> None:
        if command.name != "clear":
            # Echo the command like a shell keeps the prompt line on screen, so
            # the printed output below it reads as the answer to something.
            self.chat.add(UserMessage(f"/{command.name} {argument}".strip(), command=True))
        if command.name in _REFUSED_WHILE_RUNNING and not self._idle():
            return
        handler = getattr(self, "_cmd_" + command.name.replace("-", "_"))
        handler(argument)

    def _cmd_help(self, argument: str) -> None:
        del argument
        self._print_block(tr(self.language, "help.title"), help_text(self.language))

    def _cmd_status(self, argument: str) -> None:
        del argument
        state = self.run_state
        self._print_block(
            tr(self.language, "info.title"),
            status_text(
                lang=self.language,
                repository=self.repository,
                branch=self._branch,
                model=self._model_label(),
                session_id=self.session_id,
                effort=self.effort,
                mode=self.mode,
                tokens=self._tokens,
                verification=self._verification,
                suggested=self._detected_verification,
                counters={
                    "reads": state.reads,
                    "edits": state.edits,
                    "ok": state.tests_ok,
                    "bad": state.tests_failed,
                    "mcp": state.mcp_calls,
                    "skills": state.skill_calls,
                },
                checkpoint_at=(
                    self.checkpoint.updated_at if self.checkpoint is not None else None
                ),
            ),
        )

    def _cmd_session(self, argument: str) -> None:
        """The durable record behind this window, printed as a block.

        Read-only, so it stays available while a run is live; the record shown
        is the last checkpoint the kernel saved.
        """

        del argument
        session_id = self.session_id

        def done(record: dict[str, Any] | None) -> None:
            state = SessionState.from_record(record) if record is not None else None
            if state is not None:
                self.checkpoint = state
            self._print_block(
                tr(self.language, "session.title"),
                lines_text(session_state_lines(state, self.language)),
            )

        self._background(lambda: self.backend.session(session_id), done)

    def _cmd_doctor(self, argument: str) -> None:
        del argument

        def done(report: dict[str, Any]) -> None:
            text, _level = doctor_text(report if isinstance(report, dict) else {})
            self._print_block(tr(self.language, "doctor.title"), text)

        self._background(
            self.backend.doctor, done, activity=tr(self.language, "doctor.running")
        )

    def _cmd_reasoning(self, argument: str) -> None:
        del argument
        reasoning = self._last_reasoning or "\n\n".join(
            segment.reasoning_text() for segment in self._segments if segment.reasoning_text()
        )
        if not reasoning.strip():
            self._notice(tr(self.language, "notice.no_reasoning"), "info")
            return
        self._print_block(tr(self.language, "reasoning.title"), Text(reasoning.strip(), style=MUTED))

    def _cmd_verify(self, argument: str) -> None:
        if not argument:
            if self._verification:
                commands = "; ".join(" ".join(item) for item in self._verification)
                self._notice(tr(self.language, "notice.verify_list", commands=commands), "info")
            else:
                text = tr(self.language, "notice.verify_none")
                if self._detected_verification:
                    commands = "; ".join(" ".join(item) for item in self._detected_verification)
                    text += "\n" + tr(self.language, "welcome.facts.suggested", commands=commands)
                self._notice(text, "info")
            return
        try:
            command = tuple(shlex.split(argument))
        except ValueError as exc:
            self._notice(tr(self.language, "notice.verify_parse", error=str(exc)), "error")
            return
        if not command:
            return
        self._approve_check(command)

    def _approve_check(self, command: tuple[str, ...]) -> None:
        if command not in self._verification:
            self._verification = (*self._verification, command)
        self._notice(
            tr(self.language, "notice.verify_added", command=" ".join(command)), "success"
        )
        self._refresh_welcome()
        task, self._verify_task = self._verify_task, None
        if task:
            self._start_task(task)

    def _ask_for_check(self, task: str) -> None:
        """No check approved yet: offer the detected ones, one key to approve.

        The runtime starts an agent only with a verification command the user
        approved, and a command found in the repository is only a suggestion
        (a hostile package.json must not approve itself). So instead of a run
        that fails at once, the detected commands are offered by name; picking
        one is the approval, and the task starts right after it.
        """

        lang = self.language
        self._verify_task = task
        items = [
            MenuItem(f"cmd:{index}", " ".join(command), tr(lang, "verify.detected"))
            for index, command in enumerate(self._detected_verification)
        ]
        items.append(MenuItem("custom", tr(lang, "verify.custom"), tr(lang, "verify.custom_detail")))
        self._open_picker(
            "verify", tr(lang, "verify.title"), items, hint=tr(lang, "verify.hint")
        )

    def _verify_chosen(self, key: str, context: dict[str, Any]) -> None:
        del context
        if key == "custom":
            self.composer.set_text("/verify ")
            self._notice(tr(self.language, "notice.verify_custom"), "info")
            return
        index = int(key.partition(":")[2] or -1) if key.startswith("cmd:") else -1
        if 0 <= index < len(self._detected_verification):
            self._approve_check(self._detected_verification[index])

    def _cmd_unverify(self, argument: str) -> None:
        del argument
        self._verification = ()
        self._notice(tr(self.language, "notice.verify_cleared"), "info")
        self._refresh_welcome()

    def _cmd_effort(self, argument: str) -> None:
        if argument:
            self._set_effort(argument)
            return
        items = [MenuItem(level, level, marked=level == self.effort) for level in EFFORT_LEVELS]
        self._open_picker("effort", tr(self.language, "effort.title"), items, current=self.effort)

    def _cmd_mode(self, argument: str) -> None:
        if argument:
            self._set_mode(argument)
            return
        items = [
            MenuItem(mode, mode, tr(self.language, f"mode.{mode}"), marked=mode == self.mode)
            for mode in MODES
        ]
        self._open_picker("mode", tr(self.language, "mode.title"), items, current=self.mode)

    def _cmd_lang(self, argument: str) -> None:
        if argument:
            self._set_language(argument)
            return
        items = [
            MenuItem(code, name, code, marked=code == self.language) for code, name in LANGUAGES
        ]
        self._open_picker("lang", tr(self.language, "lang.title"), items, current=self.language)

    def _cmd_new(self, argument: str) -> None:
        del argument
        self.action_new_session()

    def _cmd_clear(self, argument: str) -> None:
        del argument
        self.action_clear()

    def _cmd_stop(self, argument: str) -> None:
        del argument
        self.action_stop()

    def _cmd_quit(self, argument: str) -> None:
        del argument
        if self._agent_running:
            self.backend.stop()
        self.exit(0)

    def _cmd_model(self, argument: str) -> None:
        if "/" in argument and " " not in argument:
            self._select_model(argument)
            return
        self._show_models(query=argument)

    def _show_models(self, *, query: str = "", current: str | None = None) -> None:
        def done(models: list[dict[str, Any]]) -> None:
            if not models:
                self._notice(tr(self.language, "notice.models_empty"), "warning")
                return
            items, selected = model_items(models, self.language)
            self._open_picker(
                "model",
                tr(self.language, "models.title"),
                items,
                current=current or selected,
                query=query,
            )

        self._background(self.backend.models, done, activity=tr(self.language, "status.loading"))

    def _model_chosen(self, key: str, context: dict[str, Any]) -> None:
        del context
        self._select_model(key)

    def _select_model(self, key: str) -> None:
        if not self._idle():
            return
        provider, _, model = key.partition("/")
        if not provider or not model:
            return

        def work() -> dict[str, Any] | None:
            self.backend.select_model(provider, model)
            return self.backend.selected_model()

        def done(selected: dict[str, Any] | None) -> None:
            self._model = selected
            self._model_loaded = True
            self._refresh_welcome()
            self._refresh_chrome()
            self._notice(tr(self.language, "models.selected", model=key), "success")

        self._background(work, done)

    def _reload_model(self) -> None:
        def done(selected: dict[str, Any] | None) -> None:
            self._model = selected
            self._model_loaded = True
            self._refresh_welcome()
            self._refresh_chrome()

        self._background(self.backend.selected_model, done)

    def _cmd_connect(self, argument: str) -> None:
        wanted = argument.strip()

        def done(rows: list[dict[str, Any]]) -> None:
            self._catalog = {
                str(row.get("provider_id")): row for row in rows if row.get("provider_id")
            }
            if wanted:
                exact = next(
                    (row for key, row in self._catalog.items() if key.lower() == wanted.lower()),
                    None,
                )
                if exact is not None:
                    self._begin_connect(exact)
                    return
            self._open_picker(
                "connect",
                tr(self.language, "connect.title"),
                catalog_items(rows, self.language),
                query=wanted,
            )

        self._background(
            self.backend.provider_catalog,
            done,
            activity=tr(self.language, "status.loading"),
            failed=lambda error: self._notice(
                tr(self.language, "notice.catalog_failed", error=error), "error"
            ),
        )

    def _provider_chosen(self, key: str, context: dict[str, Any]) -> None:
        del context
        if key == CUSTOM_PROVIDER_KEY:
            self.push_screen(
                ProviderSetupScreen(self.backend, self.language), self._provider_saved
            )
            return
        row = self._catalog.get(key)
        if row is not None:
            self._begin_connect(row)

    def _begin_connect(self, row: dict[str, Any]) -> None:
        if not self._idle():
            return
        name = str(row.get("display_name") or row.get("provider_id") or "?")
        env = [str(item) for item in row.get("env") or [] if item]
        variables = [str(item) for item in row.get("url_variables") or [] if item]
        self._connect_flow = {
            "row": row,
            "name": name,
            "key": "",
            "pending": variables,
            "values": [],
        }
        if not env:
            self._connect_next()
            return
        if row.get("connected_as"):
            placeholder = tr(self.language, "secret.placeholder_saved", provider=name)
        else:
            placeholder = tr(self.language, "secret.placeholder", provider=name, env=env[0])
        self._ask(placeholder, self._connect_key_entered, password=True)

    def _connect_key_entered(self, value: str) -> None:
        flow = self._connect_flow
        if flow is None:
            return
        key = value.strip()
        if not key and not flow["row"].get("connected_as"):
            self._connect_flow = None
            self._notice(tr(self.language, "notice.key_empty"), "warning")
            return
        flow["key"] = key
        self._connect_next()

    def _connect_next(self) -> None:
        flow = self._connect_flow
        if flow is None:
            return
        if flow["pending"]:
            name = flow["pending"][0]
            self._ask(
                tr(self.language, "secret.variable", name=name, provider=flow["name"]),
                self._connect_variable_entered,
                password=False,
                value=os.environ.get(name, ""),
            )
            return
        self._connect_flow = None
        self._run_connect(flow)

    def _connect_variable_entered(self, value: str) -> None:
        flow = self._connect_flow
        if flow is None or not flow["pending"]:
            return
        name = flow["pending"].pop(0)
        if not value.strip():
            self._connect_flow = None
            self._notice(tr(self.language, "notice.variable_empty", name=name), "warning")
            return
        flow["values"].append((name, value.strip()))
        self._connect_next()

    def _run_connect(self, flow: dict[str, Any]) -> None:
        row = flow["row"]
        name = str(flow["name"])
        catalog_id = str(row.get("provider_id") or "")
        registry_id = str(row.get("connected_as") or "")
        api_key = str(flow.pop("key", "") or "")
        variables = tuple(flow["values"])

        def work() -> dict[str, Any]:
            return self.backend.provider_connect(
                catalog_id, api_key=api_key, provider_id=registry_id, variables=variables
            )

        self._background(
            work,
            lambda payload: self._connected(name, payload),
            activity=tr(self.language, "activity.connecting", provider=name),
            failed=lambda error: self._notice(
                tr(self.language, "notice.connect_failed", provider=name, error=error), "error"
            ),
        )

    def _connected(self, name: str, payload: dict[str, Any]) -> None:
        provider_id = str(payload.get("provider_id") or "")
        model = str(payload.get("selected_model") or "")
        self._notice(
            tr(
                self.language,
                "notice.connected",
                provider=name,
                id=provider_id,
                models=payload.get("models_installed", 0),
                priced=payload.get("priced_models", 0),
            ),
            "success",
        )
        if payload.get("activated"):
            self._notice(
                tr(self.language, "models.selected", model=f"{provider_id}/{model}"), "success"
            )
            self._reload_model()
            return
        self._notice(tr(self.language, "notice.connect_pick_model", id=provider_id), "info")
        self._show_models(
            query=f"{provider_id}/", current=f"{provider_id}/{model}" if model else None
        )

    def _provider_saved(self, result: dict[str, Any] | None) -> None:
        if not result:
            return
        selected = result.get("selected_model") or result.get("model") or {}
        model = ""
        if isinstance(selected, dict):
            model = f"{selected.get('provider_id', '')}/{selected.get('model_id', '')}".strip("/")
        self._notice(tr(self.language, "notice.provider_saved", model=model or "?"), "success")
        self._reload_model()

    def _cmd_sessions(self, argument: str) -> None:
        if argument:
            self._resume(argument)
            return

        def done(rows: list[dict[str, Any]]) -> None:
            items = session_items(rows, self.repository, self.session_id, self.language)
            if not items:
                self._notice(tr(self.language, "notice.no_sessions"), "warning")
                return
            self._open_picker(
                "session",
                tr(self.language, "sessions.title"),
                items,
                current=self.session_id,
            )

        self._background(self.backend.sessions, done, activity=tr(self.language, "status.loading"))

    def _session_chosen(self, key: str, context: dict[str, Any]) -> None:
        del context
        self._resume(key)

    def _resume(self, session_id: str) -> None:
        if not self._idle():
            return
        self._background(
            lambda: self.backend.session(session_id),
            lambda record: self._resumed(session_id, record),
        )

    def _resumed(self, session_id: str, record: dict[str, Any] | None) -> None:
        if self._agent_running:
            self._notice(tr(self.language, "notice.already_running"), "warning")
            return
        if record is None:
            self._notice(tr(self.language, "notice.session_missing", id=session_id), "error")
            return
        repository = record.get("repository")
        if not same_repository(repository, self.repository):
            self._notice(
                tr(self.language, "notice.session_repo", repository=repository or "?"),
                "warning",
            )
            return
        self.session_id = session_id
        self._continue = True
        self._tokens = 0
        self.run_state = RunState()
        self.checkpoint = SessionState.from_record(record)
        self._reset_turn()
        self.chat.clear_messages()
        self._add_welcome()
        self._notice(tr(self.language, "notice.session_selected", id=session_id), "info")
        task = record.get("task")
        if isinstance(task, str) and task.strip():
            self._notice(
                tr(self.language, "notice.session_task", task=" ".join(task.split())[:200]),
                "info",
            )
        self._refresh_status()
        self._refresh_chrome()

    def _cmd_skills(self, argument: str) -> None:
        del argument

        def done(skills: list[dict[str, Any]]) -> None:
            items: list[MenuItem] = []
            for item in skills:
                name = str(item.get("name") or "")
                if not name:
                    continue
                description = " ".join(str(item.get("description") or "").split())
                if len(description) > 70:
                    description = description[:69] + "…"
                items.append(MenuItem(name, name, description))
            if not items:
                self._notice(tr(self.language, "notice.skills_empty"), "warning")
                return
            self._open_picker("skills", tr(self.language, "skills.title"), items)

        self._background(
            self.backend.skill_list, done, activity=tr(self.language, "status.loading")
        )

    def _skill_chosen(self, key: str, context: dict[str, Any]) -> None:
        del context

        def done(skill: dict[str, Any] | None) -> None:
            if skill is None:
                self._notice(tr(self.language, "notice.skill_failed", name=key), "error")
                return
            self._print_block(
                tr(self.language, "skill.detail_title", name=key),
                skill_text(skill, self.language),
            )

        self._background(lambda: self.backend.skill_show(key), done)

    def _cmd_mcp(self, argument: str) -> None:
        if argument.strip().lower() == "add":
            self._open_mcp_add()
            return
        session_id = self.session_id

        def done(payload: dict[str, Any]) -> None:
            self._mcp_payload = payload if isinstance(payload, dict) else {}
            self._open_picker(
                "mcp",
                tr(self.language, "mcp.title"),
                mcp_items(self._mcp_payload, self.language, pending=self._pending_mcp),
            )

        self._background(
            lambda: self.backend.mcp_status(session_id),
            done,
            activity=tr(self.language, "status.loading"),
        )

    def _open_mcp_add(self) -> None:
        self.push_screen(McpAddScreen(self.backend, self.language), self._mcp_added)

    def _mcp_added(self, server: str | None) -> None:
        if server:
            self._notice(tr(self.language, "notice.mcp_added", server=server), "success")

    def _mcp_in_session(self, server: str) -> bool:
        if server in self._pending_mcp:
            return True
        entries = self._mcp_payload.get("servers", [])
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and entry.get("server_id") == server:
                selection = entry.get("selection")
                state = selection.get("state") if isinstance(selection, dict) else None
                return bool(state) and state != "not_selected"
        return False

    def _mcp_server_chosen(self, key: str, context: dict[str, Any]) -> None:
        del context
        if key == ADD_SERVER_KEY:
            self._open_mcp_add()
            return
        lang = self.language
        items = [
            MenuItem("probe", tr(lang, "mcp.action.probe")),
            MenuItem("select", tr(lang, "mcp.action.select"), tr(lang, "mcp.action.select_detail")),
            MenuItem(
                "select_read",
                tr(lang, "mcp.action.select_read"),
                tr(lang, "mcp.action.select_read_detail"),
            ),
        ]
        if self._mcp_in_session(key):
            items.append(MenuItem("deselect", tr(lang, "mcp.action.deselect")))
        items.append(MenuItem("remove", tr(lang, "mcp.action.remove")))
        self._open_picker(
            "mcp-action",
            tr(lang, "mcp.actions_title", server=key),
            items,
            context={"server": key},
        )

    def _mcp_action_chosen(self, key: str, context: dict[str, Any]) -> None:
        server = str(context.get("server") or "")
        if not server:
            return
        lang = self.language
        if key == "probe":
            self._background(
                lambda: self.backend.mcp_probe(server),
                lambda payload: self._notice(
                    tr(
                        lang,
                        "notice.mcp_probe_ok",
                        server=server,
                        count=payload.get("tool_count", 0) if isinstance(payload, dict) else 0,
                    ),
                    "success",
                ),
                activity=tr(lang, "notice.mcp_probe", server=server),
                failed=lambda error: self._notice(
                    tr(lang, "notice.mcp_probe_failed", server=server, error=error),
                    "error",
                ),
            )
        elif key in {"select", "select_read"}:
            self._mcp_select(server, "all" if key == "select" else "read")
        elif key == "deselect":
            self._mcp_deselect(server)
        elif key == "remove":
            items = [
                MenuItem("no", tr(lang, "mcp.confirm_no")),
                MenuItem("yes", tr(lang, "mcp.confirm_yes")),
            ]
            self._open_picker(
                "mcp-confirm",
                tr(lang, "mcp.confirm_remove", server=server),
                items,
                context={"server": server},
            )

    def _mcp_select(self, server: str, scope: str) -> None:
        """Attach a server to this window's session: every tool, or read-only.

        The choice is the user's explicit approval, so the chosen tools are
        stored as ``allow`` -- a stored ``ask`` would hide them from the agent,
        since nothing can answer a question in the middle of a run. Before the
        first run there is no session to store it in: the server is checked
        now and attached by that run's ``--mcp-server``.
        """

        lang = self.language
        session_id = self.session_id

        def work() -> dict[str, Any]:
            if self.backend.session(session_id) is None:
                discovered = self.backend.mcp_tools(server)
                allowed = [
                    item
                    for item in discovered
                    if scope == "all" or item.get("read_only") is True
                ]
                return {"pending": True, "count": len(allowed)}
            payload = self.backend.mcp_select(server, session_id, scope)
            selection = payload.get("selection") if isinstance(payload, dict) else None
            stored = selection.get("tools") if isinstance(selection, dict) else None
            permissions = list(stored.values()) if isinstance(stored, dict) else []
            allowed_count = len(
                [
                    item
                    for item in permissions
                    if isinstance(item, dict) and item.get("permission") == "allow"
                ]
            )
            return {"pending": False, "count": allowed_count}

        def done(result: dict[str, Any]) -> None:
            count = int(result.get("count") or 0)
            if result.get("pending"):
                self._pending_mcp[server] = scope
                self._notice(
                    tr(lang, "notice.mcp_pending", server=server, count=count), "success"
                )
                return
            self._pending_mcp.pop(server, None)
            self._notice(tr(lang, "notice.mcp_selected", server=server, count=count), "success")

        self._background(
            work,
            done,
            activity=tr(lang, "notice.mcp_selecting", server=server),
            failed=lambda error: self._notice(
                tr(lang, "notice.mcp_apply_failed", server=server, error=error), "error"
            ),
        )

    def _mcp_deselect(self, server: str) -> None:
        lang = self.language
        if self._pending_mcp.pop(server, None) is not None:
            self._notice(tr(lang, "notice.mcp_deselected", server=server), "info")
            return
        session_id = self.session_id
        self._background(
            lambda: self.backend.mcp_deselect(server, session_id),
            lambda _: self._notice(tr(lang, "notice.mcp_deselected", server=server), "info"),
        )

    def _mcp_confirmed(self, key: str, context: dict[str, Any]) -> None:
        server = str(context.get("server") or "")
        if key != "yes" or not server:
            self._notice(tr(self.language, "notice.cancelled"), "info")
            return
        self._background(
            lambda: self.backend.mcp_remove(server),
            lambda _: self._notice(
                tr(self.language, "notice.mcp_removed", server=server), "success"
            ),
        )

    def _set_effort(self, argument: str) -> None:
        level = argument.strip().lower()
        if level not in EFFORT_LEVELS:
            self._notice(
                tr(self.language, "notice.effort_invalid", levels=", ".join(EFFORT_LEVELS)),
                "error",
            )
            return
        self.effort = level
        _save_preference(effort_level=level)
        self._notice(tr(self.language, "notice.effort", level=level), "info")
        self._refresh_chrome()

    def _set_mode(self, argument: str, *, quiet: bool = False) -> None:
        mode = argument.strip().lower()
        if mode not in MODES:
            self._notice(
                tr(self.language, "notice.mode_invalid", modes=", ".join(MODES)), "error"
            )
            return
        self.mode = mode
        _save_preference(agent_mode=mode)
        if not quiet:
            self._notice(tr(self.language, "notice.mode", mode=mode), "info")
        self._refresh_chrome()

    def _set_language(self, argument: str) -> None:
        selected = normalise_language(argument)
        if selected is None:
            self._notice(tr(self.language, "notice.language_invalid"), "error")
            return
        self.language = selected
        _save_preference(language=selected)
        self.composer.placeholder = tr(self.language, "composer.placeholder")
        self._refresh_status()
        self._refresh_chrome()
        self._notice(tr(self.language, "notice.language", language=selected), "info")
