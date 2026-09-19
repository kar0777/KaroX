"""Boot the real KaroX terminal application and read back what it drew.

Every existing TUI test asserts on a widget it queried by id. That catches a
missing widget and misses everything about the result a user actually sees: what
survived the window width, what got clipped, what two adjacent columns did to each
other. The selection defects recorded in ``docs/UX_BUG_INVENTORY.md`` all passed
their tests for exactly that reason -- the tests asserted that *something* was
selected and highlighted, never that it was the right something.

So this module reads the composited screen instead. ``screen_lines`` returns the
final text of every row, which is the same information a person has when looking
at the terminal, and ``assert_snapshot`` pins it.

## Determinism

A snapshot is worthless if it changes on its own. Three things in this
application move by themselves and are neutralised in ``karox_app``:

* the sponsor ticker animates, so it is switched off rather than masked -- a
  masked line still shifts the rows below it when it wraps;
* the selected model is stubbed, so no keyring or network lookup takes part;
* the repository is a temporary directory with a fixed name, because the status
  bar shows the directory's name and a random one would appear in the snapshot.

Anything else that turns out to vary belongs in ``VOLATILE`` below, with a note
on what produced it.

## Updating a snapshot

    KAROX_UPDATE_SNAPSHOTS=1 python -m unittest discover -s tests -p "test_tui_*.py"

Then read the diff before committing it. A snapshot diff is the point of the
snapshot: it is how a refactor that was supposed to change nothing proves it, and
how one that changed something says what.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Sequence
from unittest.mock import patch

from _support import (  # noqa: F401 - inserts src on sys.path
    SRC,
    _CONFIG_OVERRIDES,
    _LEGACY_OVERRIDES,
    _RUNTIME_OVERRIDES,
)

import karox.tui as tui
from karox.registry import ModelRecord

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"
UPDATE_VARIABLE = "KAROX_UPDATE_SNAPSHOTS"

# Window sizes worth pinning, and why each one.
#
# WIDE is comfortable and is what most existing tests use. STANDARD is the default
# terminal on both Windows and macOS, and is where the status bar's five equal
# columns first collide. NARROW is a split pane -- the size at which the
# conversation area was found to be two rows tall, having lost its first line off
# the top with no indication.
WIDE = (120, 42)
STANDARD = (80, 24)
NARROW = (46, 14)

VOLATILE: tuple[tuple[re.Pattern[str], str], ...] = (
    # Session identifiers are generated per run.
    (re.compile(r"\bs-[0-9a-f]{6,}\b"), "s-<id>"),
    # Elapsed times tick while the snapshot is being taken.
    (re.compile(r"\b\d+\.\d+\s*(s|с)\b"), "<elapsed>"),
)


def normalise(lines: Sequence[str]) -> list[str]:
    """Strip trailing padding and mask anything that varies between runs.

    Trailing spaces are removed because a widget pads its row to the full width,
    which makes an otherwise identical snapshot differ by invisible whitespace
    when a neighbouring column changes size.
    """
    masked: list[str] = []
    for line in lines:
        text = line.rstrip()
        for pattern, replacement in VOLATILE:
            text = pattern.sub(replacement, text)
        masked.append(text)
    return masked


def screen_lines(app: Any) -> list[str]:
    """Return the composited screen, one string per terminal row.

    ``Screen._compositor`` is the single private access in this harness, kept in
    one function so a Textual upgrade breaks here and nowhere else.
    ``Compositor.render_strips`` and ``Strip.text`` are both public; there is no
    public route to the compositor itself.
    """
    strips = app.screen._compositor.render_strips()
    return normalise([strip.text for strip in strips])


def widget_lines(widget: Any) -> list[str]:
    """Return what one widget drew by itself.

    Only meaningful for a widget that renders its own content. A container draws
    its background and border and nothing else -- its children are placed by the
    compositor, not by its ``render_line`` -- so calling this on a ``Horizontal``
    returns blank rows and any assertion over them passes for no reason. Use
    ``region_lines`` for a container.
    """
    return normalise(
        [widget.render_line(y).text for y in range(widget.size.height)]
    )


def region_lines(app: Any, widget: Any) -> list[str]:
    """Return the composited screen cropped to one widget's area.

    This is what the region really looks like, children included, which is the
    only form an assertion about a container can be made against.
    """
    region = widget.region
    rows = screen_lines(app)
    cropped = [
        rows[y][region.x : region.x + region.width]
        for y in range(region.y, min(region.y + region.height, len(rows)))
    ]
    return normalise(cropped)


def visible_text(app: Any) -> str:
    """The whole screen as one string, for a substring assertion."""
    return "\n".join(screen_lines(app))


async def settle_service_screen(screen: Any, pilot: Any, *, timeout: float = 10.0) -> None:
    """Freeze a mounted service screen after its first async state refresh.

    ``ServiceConnectScreen`` discovers saved bridge state on a worker thread and
    polls again every two seconds. Tests that inject a synthetic visible state
    must first let that initial worker finish; otherwise a slow runner can apply
    its older snapshot *after* the fixture state and hide/reorder contextual
    buttons. Stop future polling, then wait for the in-flight worker to publish
    its result. This changes no production timing and turns the tests into a
    deterministic measurement of one settled screen.
    """

    timer = getattr(screen, "_poll_timer", None)
    if timer is not None:
        with contextlib.suppress(Exception):
            timer.stop()
        screen._poll_timer = None
    deadline = time.monotonic() + timeout
    while bool(getattr(screen, "_refreshing", False)) and time.monotonic() < deadline:
        await pilot.pause(0.05)
    if bool(getattr(screen, "_refreshing", False)):
        raise AssertionError("service screen did not finish its initial async refresh")


@contextlib.contextmanager
def _temporary_directory_with_retry(timeout: float = 5.0) -> Iterator[str]:
    """Remove a test temp tree after transient Win32 handle release.

    A Textual/background worker can exit successfully while Windows still holds
    its final directory handle for a few milliseconds. Retry only the transient
    codes for a bounded interval; a persistent resource leak still fails the
    test instead of being hidden. WinError 145 ("directory is not empty")
    belongs to the same race: the tree was enumerated, a late handle released
    or recreated an entry, and the parent removal lost it.
    """

    raw = tempfile.mkdtemp()
    try:
        yield raw
    finally:
        deadline = time.monotonic() + timeout
        while True:
            try:
                shutil.rmtree(raw)
                break
            except FileNotFoundError:
                break
            except OSError as exc:
                transient = isinstance(exc, PermissionError) or (
                    getattr(exc, "winerror", None) in (5, 32, 145)
                )
                if not transient or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)


@contextlib.contextmanager
def isolated_karox_directories() -> Iterator[Path]:
    """Point every spelling of the config and runtime overrides at a temp tree.

    ``paths.py`` prefers ``KAROX_VNEXT_*`` and falls back to the shorter aliases,
    so setting one spelling and leaving another inherited redirects the app to
    whatever the developer has exported -- including their real configuration.
    """
    with _temporary_directory_with_retry() as raw:
        root = Path(raw)
        config = root / "config"
        runtime = root / "runtime"
        # A fixed name: the status bar shows the repository directory's name, and a
        # random temporary name would land in every snapshot.
        repository = root / "demo-repo"
        legacy = root / "legacy-config"
        for path in (config, runtime, repository, legacy):
            path.mkdir(parents=True, exist_ok=True)
        # The runner TEMP paths may carry a Windows 8.3 alias (RUNNER~1);
        # every consumer of the fixture names the directory the *app itself*
        # resolves, so canonicalize once here instead of in every assertion.
        repository = repository.resolve()
        environment = dict(os.environ)
        for names, value in (
            (_CONFIG_OVERRIDES, config),
            (_RUNTIME_OVERRIDES, runtime),
        ):
            for name in names:
                environment.pop(name, None)
            environment[names[0]] = str(value)
        for name in _LEGACY_OVERRIDES:
            environment.pop(name, None)
        environment[_LEGACY_OVERRIDES[0]] = str(legacy)
        with patch.dict(os.environ, environment, clear=True):
            yield repository


DEFAULT_MODEL = ModelRecord("openai", "model-a", tools="true")


@contextlib.asynccontextmanager
async def karox_app(
    *,
    size: tuple[int, int] = STANDARD,
    language: str = "ru",
    model: ModelRecord | None = DEFAULT_MODEL,
    sponsors: bool = False,
    settle: float = 0.4,
) -> AsyncIterator[tuple[Any, Any]]:
    """Yield ``(app, pilot)`` for a booted application with nothing moving.

    ``model=None`` reproduces a first run with no provider configured, which is a
    different screen rather than the same screen with a field blank.

    ``sponsors`` defaults to False so snapshots are stable, but the ticker is *on*
    by default in the product, and it costs a row. At fourteen rows that row is
    the difference between the welcome message fitting and its first line
    scrolling out of reach, so a test about layout guarantees has to ask for
    ``sponsors=True`` and get what a real user gets.
    """
    with isolated_karox_directories() as repository:
        with (
            patch.object(tui, "_selected_model", return_value=model),
            patch.object(tui, "_load_sponsors_visible", return_value=sponsors),
        ):
            app = tui.KaroXApp(repository, language=language)
            async with app.run_test(size=size) as pilot:
                await pilot.pause(settle)
                yield app, pilot


def assert_snapshot(
    case: unittest.TestCase,
    name: str,
    lines: Sequence[str],
) -> None:
    """Compare rendered lines against the stored snapshot of the same name."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f"{name}.txt"
    # Numbered, because "line 14 differs" is not something a person can find by
    # counting rows in a wall of box-drawing characters.
    body = "\n".join(f"{index:>3}|{line}" for index, line in enumerate(lines))
    if os.environ.get(UPDATE_VARIABLE):
        path.write_text(body + "\n", encoding="utf-8", newline="\n")
        return
    if not path.exists():
        case.fail(
            f"no snapshot for {name!r}. Reviewed the output and want to keep it?\n"
            f"  {UPDATE_VARIABLE}=1 python -m unittest discover -s tests "
            f'-p "test_tui_*.py"\n'
            f"Rendered:\n{body}"
        )
    expected = path.read_text(encoding="utf-8").rstrip("\n")
    case.assertEqual(
        body,
        expected,
        f"{name} no longer renders as recorded in {path.name}. If the change is "
        f"intended, re-record with {UPDATE_VARIABLE}=1 and review the diff.",
    )


__all__ = [
    "NARROW",
    "STANDARD",
    "WIDE",
    "assert_snapshot",
    "isolated_karox_directories",
    "karox_app",
    "normalise",
    "region_lines",
    "screen_lines",
    "settle_service_screen",
    "visible_text",
    "widget_lines",
]
