"""Screens of the chat for the docs: the real `karox6 chat` in a pseudo-terminal.

    python scripts/tui_frames.py docs/tui/after            # this tree's src
    python scripts/tui_frames.py /tmp/before --src /tmp/old/src

Each scenario runs the CLI against the scripted model of the tests (no network, no key),
waits for a moment worth showing and saves the screen twice: as text (``.txt``) and as a
picture with the colours the program asked for (``.svg``, dark terminal palette).
"""

from __future__ import annotations

import argparse
import html
import os
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from fake_model import FakeModel, Reply  # noqa: E402
from term import Terminal  # noqa: E402

ROWS, COLS = int(os.environ.get("FRAME_ROWS", "32")), int(os.environ.get("FRAME_COLS", "100"))

# xterm's 16 colours as Windows Terminal's "Campbell" scheme draws them.
_NAMED = {
    "black": "0c0c0c",
    "red": "c50f1f",
    "green": "13a10e",
    "brown": "c19c00",
    "yellow": "c19c00",
    "blue": "0037da",
    "magenta": "881798",
    "cyan": "3a96dd",
    "white": "cccccc",
    "brightblack": "767676",
    "brightred": "e74856",
    "brightgreen": "16c60c",
    "brightbrown": "f9f1a5",
    "brightyellow": "f9f1a5",
    "brightblue": "3b78ff",
    "brightmagenta": "b4009e",
    "brightcyan": "61d6d6",
    "brightwhite": "f2f2f2",
}
THEME = os.environ.get("FRAME_THEME", "")  # "light": KAROX_THEME=light on a white page
_FG, _BG = ("1f2328", "ffffff") if THEME == "light" else ("cccccc", "0c0c0c")

MARKDOWN_ANSWER = """\
## Done: the greeting is fixed

`hello.py` now prints **hello** and then *bye*:

- the old `print('hi')` is gone
- a second line says goodbye
  - nested item for the layout

1. run it: `python hello.py`
2. commit when happy

```python
def greet(name: str) -> str:
    # say hello
    return f"hello, {name}"  # 2 lines
```

| file | change | lines |
|------|--------|------:|
| hello.py | greeting | +2 -1 |
| README.md | none | 0 |

> Tip: `/diff` shows the whole change, `/undo` takes it back.
"""


def _color(value: str, default: str) -> str:
    if value == "default":
        return default
    if value in _NAMED:
        return _NAMED[value]
    return value if len(value) == 6 else default


def to_svg(term: Terminal) -> str:
    """The screen as an SVG picture: one <text> run per change of style."""
    cw, ch = 8.4, 18
    screen = term.screen
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{int(COLS * cw) + 16}" '
        f'height="{ROWS * ch + 16}" font-family="Cascadia Mono, Consolas, monospace" '
        f'font-size="14"><rect width="100%" height="100%" fill="#{_BG}"/>'
    ]
    for y in range(screen.lines):
        row = screen.buffer[y]
        x = 0
        while x < screen.columns:
            char = row[x]
            fg = _color(char.fg, _FG)
            bg = _color(char.bg, _BG)
            if char.reverse:
                fg, bg = bg, fg
            key = (fg, bg, char.bold, char.italics, char.underscore)
            start = x
            text = []
            while x < screen.columns:
                c = row[x]
                cfg, cbg = _color(c.fg, _FG), _color(c.bg, _BG)
                if c.reverse:
                    cfg, cbg = cbg, cfg
                if (cfg, cbg, c.bold, c.italics, c.underscore) != key:
                    break
                text.append(c.data or " ")
                x += 1
            px, py = 8 + start * cw, 8 + y * ch
            if bg != _BG:
                out.append(
                    f'<rect x="{px:.1f}" y="{py}" width="{len(text) * cw:.1f}" '
                    f'height="{ch}" fill="#{bg}"/>'
                )
            body = "".join(text)
            if body.strip():
                style = ' font-weight="bold"' if key[2] else ""
                style += ' font-style="italic"' if key[3] else ""
                style += ' text-decoration="underline"' if key[4] else ""
                out.append(
                    f'<text x="{px:.1f}" y="{py + 14}" fill="#{fg}"{style} '
                    f'xml:space="preserve">{html.escape(body)}</text>'
                )
    out.append("</svg>")
    return "\n".join(out)


class Run:
    def __init__(self, src: Path, replies: list[Reply], tmp: Path, extra: tuple[str, ...]):
        self.ws = tmp / "ws"
        self.home = tmp / "home"
        self.ws.mkdir()
        (self.ws / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        (self.ws / "docs").mkdir()
        (self.ws / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
        (self.ws / "README.md").write_text("# Demo\n", encoding="utf-8")
        import subprocess

        for args in (
            ["init", "-q", "-b", "main"],
            ["add", "-A"],
            ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
        ):
            subprocess.run(["git", *args], cwd=self.ws, check=True, capture_output=True)
        self.model = FakeModel(replies)
        self.model.__enter__()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("KAROX_", "NO_COLOR"))}
        env.pop("PYTHONUTF8", None)
        env.update(
            KAROX_HOME=str(self.home),
            KAROX_API_KEY="test-key",
            PYTHONPATH=str(src),
            TERM="xterm-256color",
            COLORTERM="truecolor",
            **({"KAROX_THEME": THEME} if THEME else {}),
        )
        argv = [sys.executable, "-m", "karox6", "chat", "--home", str(self.home)]
        argv += ["--cwd", str(self.ws), "--no-mcp", "--model", "demo-model"]
        argv += ["--base-url", self.model.url, *extra]
        self.term = Terminal(argv, rows=ROWS, cols=COLS, env=env, cwd=str(self.ws))

    def close(self) -> None:
        self.term.close()
        self.model.__exit__(None, None, None)


def _idle(term: Terminal) -> bool:
    import re

    busy = re.compile(r"\d+\.\ds  \S|esc to interrupt|Esc stops", re.IGNORECASE)
    tail = term.lines()[-6:]
    return not any(busy.search(line) for line in tail)


def _turn(run: Run, text: str, timeout: float = 40) -> None:
    asked = len(run.model.requests)
    run.term.line(text)
    end = time.time() + timeout
    while time.time() < end:
        if len(run.model.requests) > asked and _idle(run.term):
            time.sleep(0.6)
            if _idle(run.term):
                return
        time.sleep(0.1)


def scenario_welcome(run: Run) -> None:
    run.term.wait("Type a task", 30)
    time.sleep(0.8)


def scenario_turn(run: Run) -> None:
    run.term.wait("Type a task", 30)
    _turn(run, "fix the greeting in hello.py")


def scenario_commands(run: Run) -> None:
    run.term.wait("Type a task", 30)
    time.sleep(0.5)
    run.term.send("/")
    time.sleep(1.2)


def scenario_files(run: Run) -> None:
    run.term.wait("Type a task", 30)
    time.sleep(0.5)
    run.term.send("look at @")
    time.sleep(1.5)


def scenario_error(run: Run) -> None:
    run.term.wait("Type a task", 30)
    _turn(run, "hello")


def scenario_working(run: Run) -> None:
    run.term.wait("Type a task", 30)
    run.term.line("run the tests")
    time.sleep(2.5)


EDIT = {
    "file_path": "hello.py",
    "old_string": "print('hi')",
    "new_string": "print('hello')\nprint('bye')",
}
SCENARIOS: dict[str, tuple[Callable[[Run], None], list[Reply]]] = {
    "1-welcome": (scenario_welcome, []),
    "2-turn": (
        scenario_turn,
        [
            Reply(reasoning="Look first.", calls=[("read", {"file_path": "hello.py"})]),
            Reply(calls=[("bash", {"command": "python hello.py"})]),
            Reply(calls=[("edit", EDIT)]),
            Reply(text=MARKDOWN_ANSWER, chunk_size=40),
        ],
    ),
    "3-commands": (scenario_commands, []),
    "4-files": (scenario_files, []),
    "5-error": (
        scenario_error,
        [Reply(status=401, body='{"error": {"message": "invalid api key"}}')],
    ),
    "6-working": (
        scenario_working,
        [Reply(calls=[("bash", {"command": 'python -c "import time; time.sleep(30)"'})])],
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--src", type=Path, default=ROOT / "src")
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for name, (play, replies) in SCENARIOS.items():
        if args.only and args.only not in name:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            run = Run(args.src.resolve(), list(replies), Path(tmp), ())
            try:
                play(run)
                text = "\n".join(run.term.lines()).rstrip() + "\n"
                (args.out / f"{name}.txt").write_text(text, encoding="utf-8", newline="\n")
                (args.out / f"{name}.svg").write_text(
                    to_svg(run.term), encoding="utf-8", newline="\n"
                )
                print(f"{name}: ok")
            finally:
                run.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
