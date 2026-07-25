"""Capture the KaroX terminal interface headlessly as SVG.

Why SVG and not a raster image: the output is UTF-8 text, so it can be read back
through the ordinary repository read tools and reasoned about without a viewer.
Every glyph, colour and cell position is present in the markup, which is exactly
what is needed to review terminal layout.

The interface is driven through Textual's own test pilot, so no real terminal,
display or input device is required and the capture is reproducible in CI.

Usage:

    python scripts/tui_screenshot.py --out docs/tools/screenshots/start.svg
    python scripts/tui_screenshot.py --keys ctrl+b --out docs/tools/screenshots/bridge.svg
    python scripts/tui_screenshot.py --language en --size 100x30 --out out.svg

Keys are sent in order before the capture, which is how a specific screen is
reached. Use the same key names Textual uses, for example ``enter``, ``down``,
``escape`` or ``ctrl+b``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPOSITORY_ROOT / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from karox import tui  # noqa: E402  - path setup must run first


def _size(value: str) -> tuple[int, int]:
    try:
        columns, rows = value.lower().split("x", 1)
        parsed = (int(columns), int(rows))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "size must look like COLUMNSxROWS, for example 120x42"
        ) from exc
    if parsed[0] < 40 or parsed[1] < 10:
        raise argparse.ArgumentTypeError("size must be at least 40x10")
    if parsed[0] > 400 or parsed[1] > 200:
        raise argparse.ArgumentTypeError("size must be at most 400x200")
    return parsed


def _export(app: object) -> str:
    """Return the SVG for the current frame.

    Textual exposes the exporter under different names across versions, so both
    are attempted before giving up with an explicit message.
    """
    exporter = getattr(app, "export_screenshot", None)
    if callable(exporter):
        return str(exporter())
    saver = getattr(app, "save_screenshot", None)
    if callable(saver):
        return str(saver())
    raise RuntimeError(
        "the installed Textual version exposes neither export_screenshot nor "
        "save_screenshot"
    )


async def capture(
    destination: Path,
    *,
    language: str,
    size: tuple[int, int],
    keys: list[str],
    settle_frames: int,
) -> Path:
    app = tui.KaroXApp(Path.cwd(), language=language)
    async with app.run_test(size=size) as pilot:
        for _ in range(settle_frames):
            await pilot.pause()
        for key in keys:
            await pilot.press(key)
            await pilot.pause()
        markup = _export(app)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(markup, encoding="utf-8", newline="\n")
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="docs/tools/screenshots/tui.svg",
        help="where to write the SVG, relative to the working directory",
    )
    parser.add_argument("--language", default="ru", choices=("ru", "en"))
    parser.add_argument("--size", default="120x42", type=_size)
    parser.add_argument(
        "--keys",
        nargs="*",
        default=[],
        help="keys pressed in order before the capture",
    )
    parser.add_argument(
        "--settle-frames",
        default=3,
        type=int,
        help="how many frames to wait before pressing keys",
    )
    arguments = parser.parse_args(argv)
    if arguments.settle_frames < 0 or arguments.settle_frames > 100:
        parser.error("--settle-frames must be between 0 and 100")

    destination = Path(arguments.out)
    if destination.is_absolute():
        parser.error("--out must be a relative path")

    written = asyncio.run(
        capture(
            destination,
            language=arguments.language,
            size=arguments.size,
            keys=list(arguments.keys),
            settle_frames=arguments.settle_frames,
        )
    )
    print(written.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
