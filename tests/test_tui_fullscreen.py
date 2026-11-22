from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import TracebackType

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Point, Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput

from karox6 import cli
from karox6.core import events as ev
from karox6.core.messages import Usage
from karox6.core.session import SessionConfig
from karox6.providers.scripted import ScriptedProvider, Turn
from karox6.tools.base import ApprovalRequest, ToolResult
from karox6.tui import ChatApp
from karox6.tui_app import FullScreenTui


class ScreenOutput(DummyOutput):
    """DummyOutput that records alternate-screen, mouse, resize and frame text."""

    def __init__(self, rows: int = 24, columns: int = 100) -> None:
        self.size = Size(rows=rows, columns=columns)
        self.frames: list[str] = []
        self.alt_entered = 0
        self.alt_left = 0
        self.in_alt_screen = False
        self.mouse_enabled = 0
        self.mouse_disabled = 0
        self.rendered_sizes: set[Size] = set()

    def write(self, data: str) -> None:
        self.frames.append(data)

    def write_raw(self, data: str) -> None:
        self.frames.append(data)

    def enter_alternate_screen(self) -> None:
        self.alt_entered += 1
        self.in_alt_screen = True

    def quit_alternate_screen(self) -> None:
        self.alt_left += 1
        self.in_alt_screen = False

    def enable_mouse_support(self) -> None:
        self.mouse_enabled += 1

    def disable_mouse_support(self) -> None:
        self.mouse_disabled += 1

    def get_size(self) -> Size:
        return self.size

    def flush(self) -> None:
        self.rendered_sizes.add(self.size)

    @property
    def text(self) -> str:
        return "".join(self.frames)


def run_timed[T](coro: Coroutine[object, object, T], timeout: float = 15.0) -> T:
    """Run an async test body with a hard timeout so a bug fails fast instead of hanging."""
    return asyncio.run(asyncio.wait_for(coro, timeout=timeout))


async def eventually(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def rendered_rows(ui: FullScreenTui) -> list[str]:
    """Extract the currently painted lines from prompt_toolkit's rendered Screen."""
    screen = ui.app.renderer.last_rendered_screen
    if screen is None:
        return []
    rows = ui.app.output.get_size().rows
    cols = ui.app.output.get_size().columns
    out: list[str] = []
    for y in range(rows):
        row = screen.data_buffer[y]
        out.append("".join(row[x].char for x in range(cols)))
    return out


def make_full_app(
    tmp_path: Path,
    turns: list[Turn],
    ui: FullScreenTui,
    *,
    config_kwargs: dict[str, object] | None = None,
    **kwargs: object,
) -> tuple[ChatApp, ScriptedProvider]:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    cfg = SessionConfig(
        model="test-model",
        workspace=ws,
        home=tmp_path / "home",
        stream_deltas=True,
        **(config_kwargs or {}),  # type: ignore[arg-type]
    )
    provider = ScriptedProvider(list(turns))
    app = ChatApp(
        config=cfg,
        provider=provider,
        read_line=ui.read_line,
        read_answer=ui.read_answer,
        read_secret=ui.read_secret,
        write=ui.write,
        display_factory=ui.turn_display,
        color=True,
        **kwargs,  # type: ignore[arg-type]
    )
    ui.attach_app(app)
    return app, provider


# -- 1. alternate screen enter / leave (normal and exception) --------------------------


def test_alternate_screen_enters_and_leaves_on_exit_and_exception(tmp_path: Path) -> None:
    async def go() -> None:
        out = ScreenOutput()
        with create_pipe_input() as keys:
            ui = FullScreenTui(
                workspace=tmp_path,
                home=tmp_path / "home",
                input=keys,
                output=out,
                complete_in_thread=False,
            )
            async with ui:
                await eventually(lambda: out.alt_entered == 1)
                assert out.in_alt_screen is True
                assert out.mouse_enabled == 1
            assert out.alt_left == 1
            assert out.in_alt_screen is False
            assert out.mouse_disabled == 1

        # Exception inside the context must also restore the terminal cleanly.
        out2 = ScreenOutput()
        with create_pipe_input() as keys2:
            ui2 = FullScreenTui(
                workspace=tmp_path,
                home=tmp_path / "home",
                input=keys2,
                output=out2,
                complete_in_thread=False,
            )

            async def enter_and_fail() -> None:
                async with ui2:
                    await eventually(lambda: out2.alt_entered == 1)
                    assert out2.in_alt_screen is True
                    raise RuntimeError("boom")

            with pytest.raises(RuntimeError, match="boom"):
                await enter_and_fail()
            assert out2.alt_left == 1
            assert out2.in_alt_screen is False
            assert out2.mouse_disabled == 1

    run_timed(go())


# -- 2. layout (welcome at top, status bar, input at bottom) + send + streaming --------


def test_layout_bottom_input_status_bar_and_streaming(tmp_path: Path) -> None:
    async def go() -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        git = ws / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/feat-ui\n", encoding="utf-8")

        out = ScreenOutput(rows=24, columns=100)
        with create_pipe_input() as keys:
            ui = FullScreenTui(
                workspace=ws,
                home=tmp_path / "home",
                input=keys,
                output=out,
                complete_in_thread=False,
            )
            turn = Turn(
                text="Привет, мир! Streamed reply.",
                usage=Usage(input_tokens=800, output_tokens=200),
            )
            app, provider = make_full_app(tmp_path, [turn], ui)
            async with ui:
                running = asyncio.create_task(app.run())
                await eventually(
                    lambda: (
                        any("KaroX " in row for row in rendered_rows(ui))
                        and any("› " in row for row in rendered_rows(ui))
                    )
                )
                rows = rendered_rows(ui)
                # Welcome card at the top, status bar at row -2, input box at row -1.
                assert "KaroX " in rows[0]
                assert "test-model" in rows[-2]
                assert "ws (feat-ui)" in rows[-2]
                assert "approve:ask" in rows[-2]
                assert rows[-1].startswith("› ")

                # Send a Cyrillic message and verify streaming + status bar tokens update.
                keys.send_text("Покажи ответ\r")
                await eventually(
                    lambda: (
                        "Привет, мир! Streamed reply." in ui.transcript.plain_text
                        and "[completed]" in ui.transcript.plain_text
                    )
                )
                assert "› Покажи ответ" in ui.transcript.plain_text
                await eventually(
                    lambda: ui.mode == "prompt" and "1.0k tok" in rendered_rows(ui)[-2]
                )

                # Quit via Ctrl+D on empty prompt.
                keys.send_bytes(b"\x04")
                assert await asyncio.wait_for(running, 3) == 0
                assert provider.requests[0].messages[0].content == "Покажи ответ"
            assert out.in_alt_screen is False

    run_timed(go())


# -- 3. auto-scroll vs scrolled-up (PageUp, PageDown, mouse wheel, End, Ctrl+End) ------


def test_scroll_autoscroll_pageup_wheel_and_end_jump(tmp_path: Path) -> None:
    async def go() -> None:
        out = ScreenOutput(rows=12, columns=80)
        with create_pipe_input() as keys:
            ui = FullScreenTui(
                workspace=tmp_path,
                home=tmp_path / "home",
                input=keys,
                output=out,
                complete_in_thread=False,
            )
            async with ui:
                for i in range(60):
                    ui.write(f"line-{i:02d}\n")
                await eventually(lambda: any("line-59" in row for row in rendered_rows(ui)))
                assert ui.transcript.follow is True
                bottom_scroll = ui.transcript.scroll_top
                assert bottom_scroll > 0

                # PageUp scrolls up and disables auto-scroll.
                keys.send_bytes(b"\x1b[5~")
                await eventually(lambda: not ui.transcript.follow)
                scrolled_pos = ui.transcript.scroll_top
                assert scrolled_pos < bottom_scroll
                await eventually(lambda: "scrolled +" in rendered_rows(ui)[-2])

                # New streamed output arrives while scrolled up: viewport stays put!
                ui.write("line-60\nline-61\nline-62\n")
                await asyncio.sleep(0.05)
                assert ui.transcript.follow is False
                assert ui.transcript.scroll_top == scrolled_pos

                # Mouse wheel scrolls up further and down.
                ui.transcript.mouse_handler(
                    MouseEvent(
                        position=Point(0, 0),
                        event_type=MouseEventType.SCROLL_UP,
                        button=MouseButton.NONE,
                        modifiers=frozenset(),
                    )
                )
                assert ui.transcript.scroll_top == max(0, scrolled_pos - 3)
                after_wheel_up = ui.transcript.scroll_top
                ui.transcript.mouse_handler(
                    MouseEvent(
                        position=Point(0, 0),
                        event_type=MouseEventType.SCROLL_DOWN,
                        button=MouseButton.NONE,
                        modifiers=frozenset(),
                    )
                )
                assert ui.transcript.scroll_top == after_wheel_up + 3

                # End jumps back to bottom and re-enables auto-scroll.
                keys.send_bytes(b"\x1b[F")
                await eventually(lambda: ui.transcript.follow)
                await eventually(lambda: any("line-62" in row for row in rendered_rows(ui)))

                # Scroll up again and test Ctrl+End jumping back to bottom.
                keys.send_bytes(b"\x1b[5~")
                await eventually(lambda: not ui.transcript.follow)
                keys.send_bytes(b"\x1b[1;5F")
                await eventually(lambda: ui.transcript.follow)

                # New output now auto-scrolls again.
                ui.write("line-63\n")
                await eventually(lambda: any("line-63" in row for row in rendered_rows(ui)))

    run_timed(go())


# -- 4. Esc and Ctrl+C interrupt a running turn; double Ctrl+C exits -------------------


@pytest.mark.parametrize("key", [b"\x1b", b"\x03"], ids=["escape", "ctrl-c"])
def test_esc_and_ctrl_c_interrupt_running_turn(tmp_path: Path, key: bytes) -> None:
    async def go() -> None:
        started = asyncio.Event()
        cleaned = asyncio.Event()

        class SlowTool:
            name, description, parameters, read_only = "wait", "slow tool", {}, True

            async def run(self, args: dict[str, object], ctx: object) -> ToolResult:
                started.set()
                try:
                    await asyncio.sleep(3600)
                finally:
                    await asyncio.sleep(0.02)
                    cleaned.set()
                return ToolResult("unreachable")

        out = ScreenOutput()
        with create_pipe_input() as keys:
            ui = FullScreenTui(
                workspace=tmp_path / "ws",
                home=tmp_path / "home",
                input=keys,
                output=out,
                complete_in_thread=False,
            )
            app, _ = make_full_app(
                tmp_path,
                [Turn(calls=[("wait", {})]), Turn(text="recovered")],
                ui,
                extra_tools=[SlowTool()],
            )
            async with ui:
                running = asyncio.create_task(app.run())
                await eventually(lambda: ui.mode == "prompt")
                keys.send_text("start slow\r")
                await asyncio.wait_for(started.wait(), 3)
                await eventually(
                    lambda: ui.mode == "running" and "Running wait" in rendered_rows(ui)[-2]
                )
                # Interrupt the turn.
                keys.send_bytes(key)
                await eventually(lambda: "[stopped]" in ui.transcript.plain_text)
                assert cleaned.is_set()
                await eventually(lambda: ui.mode == "prompt")

                # Session stays alive for the next turn.
                keys.send_text("after\r")
                await eventually(lambda: "recovered" in ui.transcript.plain_text)
                await eventually(lambda: ui.mode == "prompt")

                # Ctrl+C on non-empty draft clears draft, then two empty Ctrl+C exit.
                keys.send_text("draft\x03")
                await eventually(lambda: ui.input_buffer.text == "")
                keys.send_bytes(b"\x03")
                await eventually(lambda: "Press Ctrl+C again to exit." in ui.transcript.plain_text)
                keys.send_bytes(b"\x03")
                assert await asyncio.wait_for(running, 3) == 0

    run_timed(go())


# -- 5. completion menu on / and @file attachment --------------------------------------


def test_slash_completion_menu_and_at_file_attachment(tmp_path: Path) -> None:
    async def go() -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "demo.py").write_text("ANSWER = 7\n", encoding="utf-8")

        out = ScreenOutput(rows=24, columns=100)
        with create_pipe_input() as keys:
            ui = FullScreenTui(
                workspace=ws,
                home=tmp_path / "home",
                input=keys,
                output=out,
                complete_in_thread=False,
            )
            app, provider = make_full_app(tmp_path, [Turn(text="Got it.")], ui)
            async with ui:
                running = asyncio.create_task(app.run())
                await eventually(lambda: ui.mode == "prompt")

                # Typing "/" opens the completion menu with descriptions above the input box.
                keys.send_text("/co")
                await eventually(
                    lambda: (
                        ui.input_buffer.complete_state is not None
                        and any("/cost" in row for row in rendered_rows(ui))
                    )
                )
                screen_text = "\n".join(rendered_rows(ui))
                assert "/cost" in screen_text
                assert "/compact" in screen_text
                assert "token" in screen_text.lower()

                # Tab selects the first completion (/cost), Enter applies it, Enter runs it.
                keys.send_text("\t\r\r")
                await eventually(lambda: "requests   0" in ui.transcript.plain_text)

                # Type an @file mention, verify completion, apply and send.
                keys.send_text("check @dem")
                await eventually(
                    lambda: (
                        (state := ui.input_buffer.complete_state) is not None
                        and any(c.text == "@demo.py" for c in state.completions)
                    )
                )
                keys.send_text("\t\r\r")
                await eventually(lambda: "Got it." in ui.transcript.plain_text)
                assert "attached: demo.py" in ui.transcript.plain_text
                assert "ANSWER = 7" in provider.requests[0].messages[0].content

                keys.send_text("/quit\r")
                assert await asyncio.wait_for(running, 3) == 0

    run_timed(go())


# -- 6. multiline input, bracketed paste, history & Ctrl+R -----------------------------


def test_multiline_paste_history_and_ctrl_r(tmp_path: Path) -> None:
    async def go() -> None:
        hist_path = tmp_path / "home" / "chat_history"
        out = ScreenOutput()
        with create_pipe_input() as keys:
            ui = FullScreenTui(
                workspace=tmp_path / "ws",
                home=tmp_path / "home",
                history=hist_path,
                input=keys,
                output=out,
                complete_in_thread=False,
            )
            app, provider = make_full_app(
                tmp_path,
                [Turn(text="r1"), Turn(text="r2"), Turn(text="r3"), Turn(text="r4")],
                ui,
            )
            async with ui:
                running = asyncio.create_task(app.run())
                await eventually(lambda: ui.mode == "prompt")

                # Alt+Enter inserts newline; Enter sends one multiline message.
                keys.send_text("first\x1b\rsecond\r")
                await eventually(lambda: "r1" in ui.transcript.plain_text)
                assert provider.requests[0].messages[0].content == "first\nsecond"

                # Ctrl+J inserts newline; Enter sends.
                keys.send_text("alpha\nbeta\r")
                await eventually(lambda: "r2" in ui.transcript.plain_text)
                assert provider.requests[1].messages[-1].content == "alpha\nbeta"

                # Bracketed paste stays a single message.
                keys.send_text("\x1b[200~p1\np2\np3\x1b[201~\r")
                await eventually(lambda: "r3" in ui.transcript.plain_text)
                assert provider.requests[2].messages[-1].content == "p1\np2\np3"

                # Ctrl+R searches history for "alpha" and re-sends it. The input line gets
                # its history back a moment after a message is sent (prompt_toolkit reloads
                # it at the next repaint, through a thread): keys sent in the same instant
                # would search an empty history.
                await eventually(lambda: ui.mode == "prompt")
                await eventually(lambda: "alpha\nbeta" in ui.input_buffer._working_lines)
                keys.send_text("\x12alpha\r\r")
                await eventually(lambda: "r4" in ui.transcript.plain_text)
                assert provider.requests[3].messages[-1].content == "alpha\nbeta"

                keys.send_text("/quit\r")
                assert await asyncio.wait_for(running, 3) == 0

    run_timed(go())


# -- 7. inline approval dialog (y / n / a / Esc) inside the full-screen app ------------


def test_approval_dialog_inside_fullscreen_app(tmp_path: Path) -> None:
    class ApprovalTool:
        name, description, parameters, read_only = "danger", "needs approval", {}, False

        async def run(self, args: dict[str, object], ctx: object) -> ToolResult:
            ok = await ctx.approve(  # type: ignore[attr-defined]
                ApprovalRequest("danger", "rm -rf build", "rm.test", "deletes files")
            )
            return ToolResult("ran" if ok else "denied by user", is_error=not ok)

    async def go() -> None:
        out = ScreenOutput()
        with create_pipe_input() as keys:
            ui = FullScreenTui(
                workspace=tmp_path / "ws",
                home=tmp_path / "home",
                input=keys,
                output=out,
                complete_in_thread=False,
            )
            turns = [Turn(calls=[("danger", {})]), Turn(text="done")] * 4
            app, _ = make_full_app(tmp_path, turns, ui, extra_tools=[ApprovalTool()])
            async with ui:
                running = asyncio.create_task(app.run())
                await eventually(lambda: ui.mode == "prompt")

                # Turn 1: answer 'n' -> denied.
                keys.send_text("one\r")
                await eventually(
                    lambda: (
                        ui.mode == "question"
                        and any("Approval required" in r for r in rendered_rows(ui))
                        and any("Run it?" in r for r in rendered_rows(ui))
                    )
                )
                assert out.in_alt_screen is True
                assert out.alt_entered == 1  # never left alternate screen
                keys.send_text("n\r")
                await eventually(lambda: ui.transcript.plain_text.count("[completed]") == 1)

                # Turn 2: answer 'y' -> ran.
                keys.send_text("two\r")
                await eventually(lambda: ui.mode == "question")
                keys.send_text("y\r")
                await eventually(lambda: ui.transcript.plain_text.count("[completed]") == 2)

                # Turn 3: answer 'a' -> always allow, status bar updates to approve:allow.
                keys.send_text("three\r")
                await eventually(lambda: ui.mode == "question")
                keys.send_text("a\r")
                await eventually(lambda: ui.transcript.plain_text.count("[completed]") == 3)
                assert app.approval_mode == "allow"
                await eventually(lambda: "approve:allow" in rendered_rows(ui)[-2])

                # Turn 4: runs without asking.
                keys.send_text("four\r")
                await eventually(lambda: ui.transcript.plain_text.count("[completed]") == 4)

                results = [m.content for m in app.session.messages if m.role == "tool"]
                assert results == ["denied by user", "ran", "ran", "ran"]

                keys.send_text("/quit\r")
                assert await asyncio.wait_for(running, 3) == 0

    run_timed(go())


# -- 8. /key hidden input (secret never echoed or rendered) ----------------------------


def test_key_command_hides_secret_and_never_echoes_it(tmp_path: Path) -> None:
    async def go() -> None:
        out = ScreenOutput()
        with create_pipe_input() as keys:
            ui = FullScreenTui(
                workspace=tmp_path / "ws",
                home=tmp_path / "home",
                input=keys,
                output=out,
                complete_in_thread=False,
            )
            app, _ = make_full_app(tmp_path, [], ui)
            async with ui:
                running = asyncio.create_task(app.run())
                await eventually(lambda: ui.mode == "prompt")
                keys.send_text("/key\r")
                await eventually(
                    lambda: (
                        ui.mode == "secret" and any("Hidden input" in r for r in rendered_rows(ui))
                    )
                )
                keys.send_text("sk-super-secret-value\r")
                await eventually(lambda: "key saved" in ui.transcript.plain_text)
                assert "sk-super-secret-value" not in ui.transcript.plain_text
                assert "sk-super-secret-value" not in out.text
                keys.send_text("/quit\r")
                assert await asyncio.wait_for(running, 3) == 0

    run_timed(go())


# -- 9. /clear clears the pane, re-shows welcome card, keeps session -------------------


def test_clear_command_clears_pane_and_keeps_session(tmp_path: Path) -> None:
    async def go() -> None:
        out = ScreenOutput()
        with create_pipe_input() as keys:
            ui = FullScreenTui(
                workspace=tmp_path / "ws",
                home=tmp_path / "home",
                input=keys,
                output=out,
                complete_in_thread=False,
            )
            app, _ = make_full_app(tmp_path, [Turn(text="unique-old-reply")], ui)
            async with ui:
                running = asyncio.create_task(app.run())
                await eventually(lambda: ui.mode == "prompt")
                keys.send_text("hello\r")
                await eventually(lambda: "unique-old-reply" in ui.transcript.plain_text)
                sid = app.session.session_id

                keys.send_text("/clear\r")
                await eventually(
                    lambda: (
                        "unique-old-reply" not in ui.transcript.plain_text
                        and f"session {sid} kept (2 messages)" in ui.transcript.plain_text
                    )
                )
                assert "KaroX " in ui.transcript.plain_text
                assert app.session.session_id == sid
                assert len(app.session.messages) == 2

                keys.send_text("/quit\r")
                assert await asyncio.wait_for(running, 3) == 0

    run_timed(go())


# -- 10. resize + unicode + control sequence sanitization ------------------------------


def test_resize_and_sanitization_of_model_control_sequences(tmp_path: Path) -> None:
    async def go() -> None:
        out = ScreenOutput(rows=20, columns=80)
        with create_pipe_input() as keys:
            ui = FullScreenTui(
                workspace=tmp_path / "ws",
                home=tmp_path / "home",
                input=keys,
                output=out,
                complete_in_thread=False,
            )
            app, _ = make_full_app(tmp_path, [], ui)
            async with ui:
                app.render(ev.TextDelta("\x1b[2J\x1b]0;pwn\x07Кириллица и 界 тест " * 10))
                app.render(ev.AssistantMessage(step=1, text="ignored"))
                await eventually(lambda: "Кириллица" in ui.transcript.plain_text)
                assert "\x1b[2J" not in ui.transcript.plain_text
                assert "pwn" not in ui.transcript.plain_text

                for size in (Size(rows=10, columns=24), Size(rows=24, columns=90)):
                    out.size = size
                    ui.invalidate()
                    await eventually(lambda s=size: s in out.rendered_sizes)
                assert "Кириллица" in ui.transcript.plain_text

    run_timed(go())


# -- 11. exit summary, TTY fullscreen default, --inline and non-TTY fallback -----------


def _install_cli_timeout(monkeypatch: pytest.MonkeyPatch, timeout: float = 15.0) -> None:
    orig_run_async = cli.run_async

    def timed_run_async[T](coro: Coroutine[object, object, T], *args: object) -> T:
        return orig_run_async(asyncio.wait_for(coro, timeout=timeout), *args)

    monkeypatch.setattr(cli, "run_async", timed_run_async)


def test_cli_fullscreen_default_prints_exit_summary_after_leaving_alt_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from karox6 import tui, tui_app

    ws = tmp_path / "ws"
    ws.mkdir()
    out = ScreenOutput()
    normal_stdout: list[str] = []
    alt_state_when_summary_printed: list[bool] = []

    def capture_stdout(text: str) -> None:
        alt_state_when_summary_printed.append(out.in_alt_screen)
        normal_stdout.append(text)

    provider = ScriptedProvider([Turn(text="done!")])
    monkeypatch.setenv("KAROX_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(tui, "stdout_write", capture_stdout)
    monkeypatch.setattr(cli, "_provider", lambda _args: provider)
    _install_cli_timeout(monkeypatch)

    async def no_mcp(*_a: object, **_kw: object) -> tuple[list[object], list[object]]:
        return [], []

    monkeypatch.setattr(cli, "_mcp_tools", no_mcp)

    driver_errors: list[BaseException] = []
    with create_pipe_input() as keys, create_app_session(input=keys, output=out):
        orig_cls = tui_app.FullScreenTui
        created: list[FullScreenTui] = []

        class TestFullScreenTui(orig_cls):
            def __init__(self, *args: object, **kwargs: object) -> None:
                kwargs["complete_in_thread"] = False
                super().__init__(*args, **kwargs)  # type: ignore[arg-type]
                self._driver_task: asyncio.Task[None] | None = None
                created.append(self)

            async def __aenter__(self) -> FullScreenTui:
                res = await super().__aenter__()

                async def drive() -> None:
                    try:
                        await eventually(lambda: self.mode == "prompt")
                        keys.send_text("hello\r")
                        await eventually(lambda: "done!" in self.transcript.plain_text)
                        await eventually(lambda: self.mode == "prompt")
                        keys.send_text("/quit\r")
                    except BaseException as exc:
                        driver_errors.append(exc)
                        if self.app.is_running and not self.app.is_done:
                            self.app.exit()

                self._driver_task = asyncio.create_task(drive())
                return res

            async def __aexit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                tb: TracebackType | None,
            ) -> None:
                if self._driver_task is not None:
                    if not self._driver_task.done():
                        self._driver_task.cancel()
                    with contextlib.suppress(BaseException):
                        await self._driver_task
                    self._driver_task = None
                await super().__aexit__(exc_type, exc, tb)

        monkeypatch.setattr(tui_app, "FullScreenTui", TestFullScreenTui)
        rc = cli.main(
            [
                "chat",
                "--model",
                "test-model",
                "--base-url",
                "http://127.0.0.1:1/v1",
                "--home",
                str(tmp_path / "home"),
                "--cwd",
                str(ws),
            ]
        )

    assert not driver_errors, f"driver failed: {driver_errors!r}"
    assert rc == 0
    assert len(created) == 1
    assert out.alt_entered == 1
    assert out.alt_left == 1
    assert out.in_alt_screen is False
    # Exit summary was written AFTER leaving the alternate screen.
    assert alt_state_when_summary_printed == [False]
    summary = "".join(normal_stdout)
    assert "karox6 chat --resume " in summary
    assert "Session " in summary


def test_cli_inline_flag_forces_inline_mode_even_on_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from karox6 import tui, tui_app

    ws = tmp_path / "ws"
    ws.mkdir()
    captured: list[str] = []
    lines = iter(("/help", "/quit"))

    async def read(prompt: str) -> str:
        captured.append(prompt)
        return next(lines)

    def fail_fullscreen(*_a: object, **_kw: object) -> None:
        pytest.fail("FullScreenTui must not be instantiated with --inline")

    monkeypatch.setenv("KAROX_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(tui_app, "FullScreenTui", fail_fullscreen)
    monkeypatch.setattr(tui, "terminal_io", lambda _h, **_kw: (read, read))
    monkeypatch.setattr(tui, "stdout_write", captured.append)
    _install_cli_timeout(monkeypatch)

    rc = cli.main(
        [
            "chat",
            "--inline",
            "--model",
            "test-model",
            "--home",
            str(tmp_path / "home"),
            "--cwd",
            str(ws),
        ]
    )
    assert rc == 0
    assert any("KaroX " in chunk for chunk in captured)


def test_cli_non_tty_falls_back_to_inline_without_fullscreen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from karox6 import tui, tui_app

    ws = tmp_path / "ws"
    ws.mkdir()
    captured: list[str] = []
    lines = iter(("/quit",))

    async def read(prompt: str) -> str:
        return next(lines)

    def fail_fullscreen(*_a: object, **_kw: object) -> None:
        pytest.fail("FullScreenTui must not be instantiated when stdin/stdout are not TTYs")

    monkeypatch.setenv("KAROX_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(tui_app, "FullScreenTui", fail_fullscreen)
    monkeypatch.setattr(tui, "terminal_io", lambda _h, **_kw: (read, read))
    monkeypatch.setattr(tui, "stdout_write", captured.append)
    _install_cli_timeout(monkeypatch)

    rc = cli.main(
        [
            "chat",
            "--model",
            "test-model",
            "--home",
            str(tmp_path / "home"),
            "--cwd",
            str(ws),
        ]
    )
    assert rc == 0
    assert any("KaroX " in chunk for chunk in captured)
