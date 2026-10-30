"""Check a browser artifact the way a browser would, statically and by rendering.

An approved check that only asks whether a file exists cannot tell a working page
from a dead one. One benchmark artifact was 1250 lines of correct-looking code
whose module never ran: it imported the bare specifier ``three`` with no import
map, so the browser refused with "Failed to resolve module specifier" and drew
nothing but the CSS background -- while every artifact-level check passed.

The static pass reads the same facts a browser refuses on, without launching
one: module imports that cannot resolve, either because a bare specifier has no
import-map entry or because a relative target does not exist. It parses text,
opens no socket and touches no file outside the repository it is given.

Static resolution is necessary and not sufficient. A later benchmark produced two
artifacts of ~1400 lines whose modules resolved, whose console stayed silent, and
which painted an empty gradient instead of an aquarium. Nothing in the source
said so. ``--render`` therefore loads the page in the already-provisioned
Chromium, records page and console errors, and measures the painted frame: a
frame with almost no local structure is a scene that never appeared. It never
downloads a browser, and reports ``skipped`` when none is installed, so a
machine without one keeps exactly the static verdict.

Usage::

    python -m karox.web_check [--root ROOT] [--render] [PAGE ...]

Exit code 0 only when every checked page resolves its imports and, when rendering
was requested and possible, paints something and stays silent; the verdict goes
to stdout as JSON, the same shape other checks use.
"""

from __future__ import annotations

import argparse
import atexit
import base64
import json
import os
import pathlib
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Optional, Sequence

_MODULE_BLOCK = re.compile(
    r"<script[^>]*type\s*=\s*[\"']module[\"'][^>]*>(.*?)</script>", re.S | re.I
)
_IMPORT_MAP = re.compile(
    r"<script[^>]*type\s*=\s*[\"']importmap[\"'][^>]*>(.*?)</script>", re.S | re.I
)
# Matches `import x from "s"`, `export { y } from "s"` and the side-effect form
# `import "s"`. A static `import(` call is not a module specifier for the loader.
_FROM = re.compile(r"\b(?:import|export)\b[^;]*?\bfrom\s*[\"']([^\"']+)[\"']")
_BARE_SIDE_EFFECT = re.compile(r"\bimport\s*[\"']([^\"']+)[\"']")
_MAX_PAGES = 20

#: How long a page gets to reach its first painted frames before the frame is
#: measured. Headless rendering of a WebGL scene needs a real animation tick.
RENDER_SETTLE_MS = 2500
#: Fraction of sampled pixels that must differ from their neighbour eight pixels
#: away for the frame to count as painted. Measured on four benchmark artifacts
#: of one task: the two that drew nothing scored 0.003 and 0.004, the two that
#: drew the scene scored 0.072 and 0.114 -- so this floor sits a factor of two
#: above the emptiest frame and a factor of seven below the poorest real one.
RENDER_DETAIL_FLOOR = 0.01
#: Share of the drawing surface that may still match the page background before
#: the canvas counts as invisible. Measured on real artifacts: a canvas cleared to
#: a flat colour differs from the page behind it on every sample, while an empty
#: canvas over a decorative backdrop matches it on nearly all of them.
_CANVAS_BACKGROUND_SHARE = 0.98
#: Share of the bottom quarter of the frame that may be almost black before the
#: frame is reported. A scene whose floor, ground or reflector is missing reads
#: this way: the camera still looks at something, so the canvas is neither blank
#: nor flat, and the earlier verdicts all pass it.
DARK_BOTTOM_SHARE = 0.6
#: Brightness (0-1) below which a pixel counts as almost black.
DARK_LUMA = 0.08
#: Standard deviation of brightness below which the frame is one tone. A hundred
#: distinct dark colours are still an unlit scene, which a count of tones cannot
#: tell.
FLAT_LUMA_STD = 0.03
#: A 3D scene can have enough edge structure to clear the blank-frame floor while
#: still being hidden behind one large dark surface. The failed aquarium benchmark
#: measured luma=0.1433, std=0.0784, dominant_share=0.3945 and detail=0.0203: the
#: black front "seam" occupied most of the useful view while fish and plants were
#: technically rendering behind it. These thresholds are conjunctive so an
#: intentionally dark but detailed/contrasty scene is not rejected.
OCCLUDED_LUMA_MEAN = 0.18
OCCLUDED_LUMA_STD = 0.09
OCCLUDED_DOMINANT_SHARE = 0.32
OCCLUDED_DETAIL_CEILING = 0.03
#: How many pixels of the bottom quarter must be sampled before the dark-bottom
#: verdict is stated: a handful of samples is not a frame.
_MIN_BOTTOM_SAMPLES = 20
RENDER_VIEWPORT_WIDTH = 1280
RENDER_VIEWPORT_HEIGHT = 800
_MAX_RENDERED_LINES = 5
#: A screenshot handed back to the model is a small image, not an archive: past
#: this the run pays more for the picture than for the work it describes.
_MAX_SCREENSHOT_BYTES = 900_000


#: The browser a process keeps alive between renders. Launching Chromium costs
#: about a second, and a run that renders its page once per check paid that cost
#: on every check. One browser per process, closed at exit; a failed launch is
#: never cached, so a machine that cannot run it is retried rather than poisoned.
_LAUNCH_ARGS = ["--disable-gpu", "--enable-unsafe-swiftshader"]
_browser_lock = threading.Lock()
_browser_state: dict[str, Any] = {"playwright": None, "browser": None}
#: The one thread that owns the browser. Playwright's synchronous API installs an
#: event loop in the thread that starts it and keeps it for the life of the
#: process; doing that in a caller's thread left every later async test -- and any
#: async host such as the TUI -- failing with "cannot be called from a running
#: event loop". So the browser lives in a worker thread of its own and callers
#: only ever see its results.
_browser_executor: Optional[ThreadPoolExecutor] = None


def _browser_thread() -> ThreadPoolExecutor:
    global _browser_executor
    with _browser_lock:
        if _browser_executor is None:
            _browser_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="karox-browser"
            )
        return _browser_executor


def _closed_executor() -> bool:
    with _browser_lock:
        return _browser_executor is not None and getattr(
            _browser_executor, "_shutdown", False
        )


def _run_in_browser_thread(work: Callable[[], Any]) -> Any:
    """Run ``work`` in the browser thread, or raise when this process has none."""
    if _closed_executor():
        raise RuntimeError("the browser thread has been shut down")
    return _browser_thread().submit(work).result()


def _shared_browser() -> Optional[tuple[Any, Any]]:
    """The process-wide Chromium, launched on first use, or ``None``.

    ``None`` means this machine cannot render right now, and every caller treats
    that the way it treated a missing browser before: skip, never guess.
    """
    with _browser_lock:
        browser = _browser_state.get("browser")
        if browser is not None:
            try:
                if browser.is_connected():
                    return _browser_state["playwright"], browser
            except Exception:
                pass
        _close_shared_browser_locked()
        try:
            from playwright.sync_api import sync_playwright

            playwright = sync_playwright().start()
        except Exception:
            return None
        try:
            browser = playwright.chromium.launch(args=list(_LAUNCH_ARGS))
        except Exception:
            try:
                playwright.stop()
            except Exception:
                pass
            return None
        _browser_state["playwright"] = playwright
        _browser_state["browser"] = browser
        return playwright, browser


def _close_shared_browser_locked() -> None:
    """Drop the shared browser and its playwright, ignoring every teardown error.

    Called under ``_browser_lock`` (or at exit, when nothing else is running).
    """
    browser = _browser_state.get("browser")
    playwright = _browser_state.get("playwright")
    _browser_state["browser"] = None
    _browser_state["playwright"] = None
    for item, closer in ((browser, "close"), (playwright, "stop")):
        if item is None:
            continue
        try:
            getattr(item, closer)()
        except Exception:
            pass


def _close_shared_browser() -> None:
    """Close the shared browser, and its thread, at process exit."""
    global _browser_executor
    with _browser_lock:
        executor = _browser_executor
        _browser_executor = None
    if executor is not None:
        try:
            executor.submit(_close_shared_browser_locked).result(timeout=30)
        except Exception:
            pass
        executor.shutdown(wait=False, cancel_futures=True)
    with _browser_lock:
        _close_shared_browser_locked()


atexit.register(_close_shared_browser)


def _new_page(browser: Any) -> Any:
    return browser.new_page(
        viewport={"width": RENDER_VIEWPORT_WIDTH, "height": RENDER_VIEWPORT_HEIGHT}
    )


def _screenshot_in_browser(page: pathlib.Path) -> Optional[bytes]:
    """Take one frame in the browser thread. Never raises."""
    shared = _shared_browser()
    if shared is None:
        return None
    _playwright, browser = shared
    view = _new_page(browser)
    try:
        view.goto(page.resolve().as_uri())
        view.wait_for_timeout(RENDER_SETTLE_MS)
        payload = view.screenshot()
    finally:
        view.close()
    if not payload or len(payload) > _MAX_SCREENSHOT_BYTES:
        return None
    return payload



# The metric runs in the browser: it decodes the screenshot it is handed and
# reports local structure, tone count and how much of the *drawing surface* still
# looks like the page behind it, so this check needs no image library.
#
# Structure alone was the wrong verdict and this file proves it: a page whose
# canvas was cleared to a flat magenta measured as "blank" (a flat colour has no
# local variation) while a full-viewport decorative wallpaper measured as the
# best frame of the whole comparison. A run noticed exactly that, gave up on the
# 3D scene it could not verify, and shipped the wallpaper. What makes a drawing
# visible is that it differs from what would have been there without it, so the
# verdict is now "the canvas region does not look like the page background".
_PIXEL_STATS_JS = """
async (payload) => {
  const { source, rect, background } = payload;
  const image = new Image();
  image.src = source;
  await image.decode();
  const canvas = document.createElement("canvas");
  canvas.width = image.naturalWidth;
  canvas.height = image.naturalHeight;
  const context = canvas.getContext("2d");
  context.drawImage(image, 0, 0);
  const { data } = context.getImageData(0, 0, canvas.width, canvas.height);
  const width = canvas.width;
  const parse = (value) => {
    const parts = String(value).match(/(\\d+)[,\\s]+(\\d+)[,\\s]+(\\d+)/);
    return parts ? [Number(parts[1]), Number(parts[2]), Number(parts[3])] : null;
  };
  const bg = parse(background);
  let left = 0, top = 0, right = width, bottom = canvas.height;
  if (rect && rect.width > 1 && rect.height > 1) {
    left = Math.max(0, Math.min(width - 1, Math.round(rect.x)));
    top = Math.max(0, Math.min(canvas.height - 1, Math.round(rect.y)));
    right = Math.max(left + 1, Math.min(width, Math.round(rect.x + rect.width)));
    bottom = Math.max(top + 1, Math.min(canvas.height, Math.round(rect.y + rect.height)));
  }
  const tones = new Set();
  const tones_histogram = {};
  let sampled = 0;
  let structured = 0;
  let background_like = 0;
  // Brightness, not colour: a frame can hold many colours and still be unlit.
  // The bottom quarter is tracked on the frame's own height, so a scene whose
  // camera points away from its tank floor cannot hide behind a bright top.
  const dark_luma = Number(payload.dark_luma);
  const bottom_edge = canvas.height * 0.75;
  let luma_sum = 0;
  let luma_square_sum = 0;
  let bottom_sampled = 0;
  let dark_bottom = 0;
  for (let y = top + 4; y + 4 < bottom; y += 8) {
    for (let x = left + 4; x + 12 < right; x += 8) {
      const here = (y * width + x) * 4;
      const there = (y * width + x + 8) * 4;
      tones.add(((data[here] >> 3) << 10) | ((data[here + 1] >> 3) << 5) |
                (data[here + 2] >> 3));
      const delta = Math.abs(data[here] - data[there]) +
                    Math.abs(data[here + 1] - data[there + 1]) +
                    Math.abs(data[here + 2] - data[there + 2]);
      if (delta > 24) {
        structured += 1;
      }
      if (bg) {
        const gap = Math.abs(data[here] - bg[0]) + Math.abs(data[here + 1] - bg[1]) +
                    Math.abs(data[here + 2] - bg[2]);
        if (gap <= 24) {
          background_like += 1;
        }
      }
      const tone = ((data[here] >> 5) << 10) | ((data[here + 1] >> 5) << 5) |
                   (data[here + 2] >> 5);
      tones_histogram[tone] = (tones_histogram[tone] || 0) + 1;
      const luma = (0.2126 * data[here] + 0.7152 * data[here + 1] +
                    0.0722 * data[here + 2]) / 255;
      luma_sum += luma;
      luma_square_sum += luma * luma;
      if (y >= bottom_edge) {
        bottom_sampled += 1;
        if (luma < dark_luma) {
          dark_bottom += 1;
        }
      }
      sampled += 1;
    }
  }
  let modal_tone = null;
  let modal_count = 0;
  for (const [tone, count] of Object.entries(tones_histogram)) {
    if (count > modal_count) {
      modal_count = count;
      modal_tone = Number(tone);
    }
  }
  // What one flat colour the frame is, and how much of it that colour is: a
  // scene that renders as a single tone is the failure this reports, and it used
  // to be invisible in the verdict -- the numbers said "not the page background"
  // while the frame held one colour.
  const modal_color = modal_tone === null ? null : [
    ((modal_tone >> 10) & 31) << 3, ((modal_tone >> 5) & 31) << 3, (modal_tone & 31) << 3,
  ].join(",");
  const luma_mean = sampled ? luma_sum / sampled : null;
  // Population standard deviation of brightness: "nearly one tone" as a number
  // rather than as a colour count, because a hundred tones that are all black
  // are still an unlit frame.
  const luma_std = luma_mean === null
    ? null
    : Math.sqrt(Math.max(0, luma_square_sum / sampled - luma_mean * luma_mean));
  return {
    sampled,
    structured,
    tones: tones.size,
    background_like,
    dominant_color: modal_color,
    dominant_share: sampled ? Number((modal_count / sampled).toFixed(4)) : null,
    bottom_sampled,
    dark_bottom_share: bottom_sampled
      ? Number((dark_bottom / bottom_sampled).toFixed(4))
      : null,
    luma_mean: luma_mean === null ? null : Number(luma_mean.toFixed(4)),
    luma_std: luma_std === null ? null : Number(luma_std.toFixed(4)),
    canvas_rect: rect || null,
    background: bg ? bg.join(",") : null,
  };
}
"""

#: What the page says about its own drawing surface, read before the frame is
#: measured: the largest canvas is what a viewer's eye lands on, and the page's
#: background colour is what an unpainted canvas would show instead.
_PAGE_PROBE_JS = """
() => {
  const paints_with_three = Array.from(document.querySelectorAll("script")).some(
    (script) => /three(\\.module)?\\.js|["']three["']/.test(script.textContent || "")
  );
  let rect = null;
  let area = 0;
  for (const element of document.querySelectorAll("canvas")) {
    const box = element.getBoundingClientRect();
    if (box.width * box.height > area) {
      area = box.width * box.height;
      rect = { x: box.x, y: box.y, width: box.width, height: box.height };
    }
  }
  const background =
    getComputedStyle(document.body).backgroundColor ||
    getComputedStyle(document.documentElement).backgroundColor || "";
  return {
    canvases: document.querySelectorAll("canvas").length,
    canvas_rect: rect,
    paints_with_three,
    // Named for what it is: the colour of the page *behind* the canvas. A
    // measured run read a field called `background` as the colour of the frame
    // it had just been handed, concluded the capture held only the CSS
    // background, and spent its last hour tuning a scene it believed was not
    // being photographed.
    page_background: background,
  };
}
"""


def import_map_targets(text: str) -> dict[str, str]:
    """Every specifier prefix the page's import maps define."""

    targets: dict[str, str] = {}
    for block in _IMPORT_MAP.findall(text):
        try:
            payload = json.loads(block)
        except ValueError:
            continue
        imports = payload.get("imports") if isinstance(payload, dict) else None
        if isinstance(imports, dict):
            for key, value in imports.items():
                if isinstance(key, str) and isinstance(value, str) and key:
                    targets.setdefault(key, value)
    return targets


def module_specifiers(text: str) -> list[str]:
    specifiers: list[str] = []
    for block in _MODULE_BLOCK.findall(text):
        for match in _FROM.finditer(block):
            specifiers.append(match.group(1))
        for match in _BARE_SIDE_EFFECT.finditer(block):
            specifiers.append(match.group(1))
    return specifiers


def _resolves_bare(specifier: str, targets: dict[str, str]) -> bool:
    if specifier in targets:
        return True
    # A trailing-slash key maps a whole subtree: "three/addons/" -> ".../jsm/".
    return any(
        key.endswith("/") and specifier.startswith(key) for key in targets
    )


# A module fetched by URL can itself import a bare specifier, and the page cannot
# know that without fetching it. The three.js addons are the case this benchmark
# hit twice: OrbitControls.js imports `three`, so a page that loads it by URL and
# defines no import map fails with "Failed to resolve module specifier "three""
# even though every specifier the page itself wrote is a full URL.
_ADDONS = re.compile(r"https?://[^\"']*three@[^\"']*/(?:examples/jsm|addons)/")


def _addons_without_three_mapping(text: str, targets: dict[str, str]) -> list[str]:
    if "three" in targets:
        return []
    # A "three/" prefix entry is not enough: the addon module itself imports the
    # bare specifier "three", which a trailing-slash mapping cannot resolve.
    problems: list[str] = []

    def addon(specifier: str) -> bool:
        return bool(
            _ADDONS.search(specifier)
            or specifier.startswith(("three/addons/", "three/examples/jsm/"))
        )

    for block in _MODULE_BLOCK.findall(text):
        for match in _FROM.finditer(block):
            specifier = match.group(1)
            if addon(specifier):
                problems.append(specifier)
        for match in _BARE_SIDE_EFFECT.finditer(block):
            specifier = match.group(1)
            if addon(specifier):
                problems.append(specifier)
    return problems


def check_page(page: pathlib.Path) -> list[str]:
    """Return every import this page cannot resolve, as user-facing lines."""

    try:
        text = page.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return [f"{page.name}: cannot be read ({type(error).__name__})"]
    targets = import_map_targets(text)
    problems: list[str] = []
    for specifier in module_specifiers(text):
        if specifier.startswith(("http://", "https://", "data:", "//")):
            continue
        if specifier.startswith(("./", "../", "/")):
            if specifier.startswith("/"):
                # An absolute path is served from a server root this check cannot
                # know; only a relative target can be proved missing here.
                continue
            target = (page.parent / specifier).resolve()
            if not target.exists():
                problems.append(
                    f"{page.name}: imports {specifier!r}, which does not exist"
                )
            continue
        if not _resolves_bare(specifier, targets):
            problems.append(
                f"{page.name}: imports the bare specifier {specifier!r} with no "
                "import-map entry; a browser cannot resolve it"
            )
    for specifier in _addons_without_three_mapping(text, targets):
        problems.append(
            f"{page.name}: loads {specifier!r}, whose own module imports 'three', "
            "and the page defines no import map for it; the browser will refuse "
            "with \"Failed to resolve module specifier \\\"three\\\"\""
        )
    for problem in _document_problems(page.name, text):
        problems.append(problem)
    for problem in _duplicate_declarations(page.name, text):
        problems.append(problem)
    return problems


class _DocumentEndParser(HTMLParser):
    """Recognize markup end tags, not raw-text/RCDATA content."""

    _TEXT_ELEMENTS = {
        "script", "style", "textarea", "title", "xmp", "iframe", "noembed", "noframes",
    }

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._TEXT_ELEMENTS:
            self.set_cdata_mode(tag)

    def __init__(self) -> None:
        super().__init__()
        self.closed_html = False

    def handle_endtag(self, tag: str) -> None:
        if tag == "html":
            self.closed_html = True


_JS_DECLARATION = re.compile(
    r"(?:export\s+)?(?:const|let|class)\s+([A-Za-z_$][\w$]*)"
)


def _top_level_declarations(source: str) -> list[str]:
    """Names declared at one script's top level, in order.

    A small scanner rather than a parser: strings and comments are skipped and
    braces track depth, which is enough to tell a module-level ``const FISH_L``
    from a local of the same name inside a function (legal shadowing).
    """

    names: list[str] = []
    depth = 0
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char in "\"'`":
            quote = char
            index += 1
            while index < length:
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == quote:
                    index += 1
                    break
                index += 1
            continue
        if char == "/" and index + 1 < length:
            if source[index + 1] == "/":
                newline = source.find("\n", index)
                index = length if newline < 0 else newline + 1
                continue
            if source[index + 1] == "*":
                end = source.find("*/", index + 2)
                index = length if end < 0 else end + 2
                continue
        if char == "{":
            depth += 1
            index += 1
            continue
        if char == "}":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth == 0:
            match = _JS_DECLARATION.match(source, index)
            if match is not None:
                prefix = source[max(0, index - 24) : index].rstrip()
                if prefix.endswith(("for (", "for(", "for await (", "for await(")):
                    # `for (let i ...)` and `for (const sx of ...)` bind inside
                    # the loop statement; several such loops at module level are
                    # legal and must not be reported as redeclarations.
                    index = match.end()
                    continue
                names.append(match.group(1))
                index = match.end()
                continue
        index += 1
    return names


class _ScriptBlockParser(HTMLParser):
    """Collect inline script bodies; external ``src`` scripts are not read."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self._buffer: list[str] | None = None
        self._external = False

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        if tag.lower() != "script":
            return
        attributes = {key.lower(): (value or "") for key, value in attrs}
        self._external = bool(attributes.get("src"))
        self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._buffer is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "script" or self._buffer is None:
            return
        if not self._external:
            self.blocks.append("".join(self._buffer))
        self._buffer = None
        self._external = False


def _duplicate_declarations(name: str, text: str) -> list[str]:
    """Top-level names a browser would reject as already declared.

    Two aquarium runs added a second ``const FISH_L`` / ``const
    waterVolumeMaterial`` while appending a section; the page threw before the
    scene ran and only a visual check could see it. This is the static guard,
    measured on the file the mutation just wrote.
    """

    parser = _ScriptBlockParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:  # noqa: BLE001 - a malformed page is reported elsewhere
        return []
    problems: list[str] = []
    for source in parser.blocks:
        seen: set[str] = set()
        reported: set[str] = set()
        for declared in _top_level_declarations(source):
            if declared in seen and declared not in reported:
                reported.add(declared)
                problems.append(
                    f"{name}: declares '{declared}' more than once at top level; "
                    f"the browser throws \"Identifier '{declared}' has already "
                    "been declared\" before the page runs"
                )
            seen.add(declared)
    return problems


def _document_problems(name: str, text: str) -> list[str]:
    """Whether this is one complete document rather than the middle of one.

    A browser renders a page that lost its ending, which is how a run's edit
    spree shipped a file with no closing ``</html>`` and a check that passed it.
    Leaving the work half-typed is the failure mode, so it is named here rather
    than left for a reader of the file to notice.
    """

    problems: list[str] = []
    stripped = text.strip()
    if not stripped:
        return [f"{name}: the file is empty"]
    if not stripped.lower().startswith("<!doctype html"):
        problems.append(
            f"{name}: does not start with <!doctype html>, so this is not one "
            "complete HTML document"
        )
    document = _DocumentEndParser()
    document.feed(stripped)
    document.close()
    if not document.closed_html:
        problems.append(
            f"{name}: has no closing </html>, so the document was cut short; the "
            "browser will still render what is there, which is why it has to be "
            "said out loud"
        )
    return problems


def render_problems(
    name: str,
    *,
    page_errors: Sequence[str] = (),
    console_errors: Sequence[str] = (),
    measured: dict | None = None,
    probe: dict | None = None,
    resize: dict | None = None,
) -> dict:
    """Turn what a render observed into a verdict, without touching a browser.

    Kept pure so the thresholds and the wording are testable on the numbers the
    calibration measured, while the browser I/O stays in :func:`render_page`.
    """

    problems: list[str] = []
    for text in page_errors:
        problems.append(f"{name}: the page threw during load: {text[:300]}")
    for text in list(console_errors)[:_MAX_RENDERED_LINES]:
        problems.append(f"{name}: console error: {text[:300]}")
    result: dict[str, Any] = {
        "console_errors": list(console_errors)[:_MAX_RENDERED_LINES],
        "page_errors": list(page_errors)[:_MAX_RENDERED_LINES],
    }
    # Only observation fields may cross this boundary. A measurement cannot
    # supply its own verdict or overwrite the errors observed by the caller.
    if not isinstance(measured, dict):
        problems.append(f"{name}: invalid render measurement; expected an object")
        measured = {}
    result.update({
        key: measured[key]
        for key in (
            "sampled", "structured", "tones", "background_like",
            "dominant_color", "dominant_share", "canvas_rect", "background",
            "bottom_sampled", "dark_bottom_share", "luma_mean", "luma_std",
        )
        if key in measured
    })
    if not isinstance(probe, dict):
        problems.append(f"{name}: invalid page probe; expected an object")
        probe = {}
    if probe:
        result.update(
            {
                "canvases": probe.get("canvases"),
                "canvas_rect": probe.get("canvas_rect"),
                "paints_with_three": probe.get("paints_with_three"),
            }
        )
    if isinstance(resize, dict):
        result["resize"] = {
            "ok": resize.get("ok"),
            "target": resize.get("target"),
            "after": resize.get("after"),
            "error": resize.get("error"),
            "reason": resize.get("reason"),
        }
        if resize.get("ok") is False:
            raw_after = resize.get("after")
            after = raw_after if isinstance(raw_after, dict) else {}
            raw_canvas = after.get("canvas")
            canvas = raw_canvas if isinstance(raw_canvas, dict) else {}
            problems.append(
                f"{name}: after the viewport changed to 1000x700 the canvas "
                f"stayed {canvas.get('clientWidth')}x{canvas.get('clientHeight')}"
            )
    raw_sampled = result.get("sampled")
    sampled = (
        raw_sampled
        if isinstance(raw_sampled, int) and not isinstance(raw_sampled, bool)
        else 0
    )
    if sampled <= 0:
        sampled = 0
        problems.append(
            f"{name}: render measurement contains no valid sampled pixels; "
            "the rendered page has not been verified"
        )
    def valid_count(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    structured = result.get("structured")
    background_like = result.get("background_like")
    counts_valid = True
    for field, value in (("structured", structured), ("background_like", background_like)):
        if not isinstance(value, int) or not valid_count(value) or value > sampled:
            problems.append(f"{name}: invalid render measurement {field}; expected a count within sampled pixels")
            counts_valid = False
    canvases = probe.get("canvases")
    # Older callers omit this optional flag; an explicitly malformed flag is
    # not equivalent to a page that does not import a renderer.
    draws_with_three = probe.get("paints_with_three", False)
    probe_valid = valid_count(canvases) and isinstance(draws_with_three, bool)
    if not probe_valid:
        problems.append(f"{name}: invalid page probe canvases or paints_with_three")
    blank = False
    if sampled and counts_valid and probe_valid:
        assert isinstance(structured, int) and isinstance(background_like, int)
        result["detail"] = round(structured / sampled, 4)
        result["structure"] = result["detail"]
        if canvases:
            # The drawing surface must change what the viewer sees. A canvas
            # indistinguishable from the page behind it paints nothing, however
            # much structure the rest of the frame has -- and a decorative
            # wallpaper behind an empty canvas is exactly the case that used to
            # score highest.
            share = background_like / sampled
            result["background_share"] = round(share, 4)
            blank = share > _CANVAS_BACKGROUND_SHARE
            if (
                not blank
                and draws_with_three
                and result["detail"] < RENDER_DETAIL_FLOOR
            ):
                # A page that renders a 3D scene and paints one flat tone has not
                # drawn its scene, whatever colour that tone is. Measured: a run
                # whose aquarium rendered as a near-uniform dark field passed
                # this check on "the canvas is not the page background", spent
                # its remaining hour tuning a scene that was not appearing, and
                # shipped it. A flat colour is only evidence of a drawing when
                # the page is not trying to draw a scene.
                problems.append(
                    f"{name}: the page renders a 3D scene but the frame is "
                    f"essentially one colour ({result.get('dominant_share')} of the "
                    f"canvas area is {result.get('dominant_color')}, "
                    f"{result['detail']:.4f} local structure, "
                    f"{result.get('tones')} distinct tones); check the exposure, the "
                    "fog, the camera distance and whether the objects are inside the "
                    "frustum, and compare the frame against the task rather than "
                    "against the previous frame"
                )
        elif canvases == 0 and draws_with_three:
            problems.append(
                f"{name}: imports a 3D renderer but creates no canvas, so nothing "
                "it draws can reach the screen"
            )
        result["blank"] = blank
        if blank:
            problems.append(
                f"{name}: the canvas paints nothing visible ({result['detail']:.4f} "
                f"local structure, {result.get('tones')} distinct tones, "
                f"{result.get('background_share')} of the surface still matches the "
                "page background); the module may run while the scene stays empty, "
                "so check that the object the task asks for is added to the scene, "
                "is inside the camera frustum at the starting position, and is lit"
            )
        dark_share = result.get("dark_bottom_share")
        bottom_sampled = result.get("bottom_sampled")
        if (
            # A 3D scene whose floor was never drawn or lit from below is
            # unreadable. A 2D board with margins legitimately has a dark band
            # below it: the Snake page passed its grader while this rule kept
            # reporting the page background and spent the whole visual budget.
            draws_with_three
            and isinstance(dark_share, float)
            and isinstance(bottom_sampled, int)
            and not isinstance(bottom_sampled, bool)
            and bottom_sampled >= _MIN_BOTTOM_SAMPLES
        ):
            if dark_share > DARK_BOTTOM_SHARE:
                # Nothing in the earlier verdicts looks at where the light falls:
                # a scene lit from below, or one whose floor was never drawn, can
                # be neither blank nor flat and still be unreadable.
                problems.append(
                    f"{name}: bottom quarter of the frame is almost black "
                    f"({dark_share:.0%})"
                )
        luma_mean = result.get("luma_mean")
        luma_std = result.get("luma_std")
        dominant_share = result.get("dominant_share")
        if isinstance(luma_std, float) and luma_std < FLAT_LUMA_STD:
            problems.append(
                f"{name}: frame is nearly uniform; scene may not be lit or rendered"
            )
        if (
            draws_with_three
            and isinstance(luma_mean, float)
            and isinstance(luma_std, float)
            and isinstance(dominant_share, float)
            and luma_mean < OCCLUDED_LUMA_MEAN
            and FLAT_LUMA_STD <= luma_std < OCCLUDED_LUMA_STD
            and dominant_share > OCCLUDED_DOMINANT_SHARE
            and result["detail"] < OCCLUDED_DETAIL_CEILING
        ):
            problems.append(
                f"{name}: 3D frame is dominated by a dark near-flat surface "
                f"(mean luma {luma_mean:.3f}, dominant share {dominant_share:.0%}, "
                f"local structure {result['detail']:.4f}); the scene may be hidden "
                "behind an opaque/oversized mesh or severely underexposed"
            )
    # Painted is the absence of every objection, not of the blankness one alone:
    # a page that imports a renderer and creates no canvas has a problem too.
    if not problems:
        result["painted"] = True
    result["problems"] = problems
    return result


def pixel_payload(*, probe: dict | None, shot: bytes) -> dict:
    """The arguments the pixel pass needs, read from the page probe.

    A named function rather than an inline dict because the two sides are edited
    apart: renaming the probe's ``background`` to ``page_background`` left this
    lookup pointing at the old name, ``background_like`` silently stayed at zero
    for every page, and the criterion that catches an empty canvas over a
    decorative backdrop stopped being able to fire at all. A test at this
    boundary is what makes that a caught mistake instead of a quiet one.
    """

    return {
        "source": "data:image/png;base64," + base64.b64encode(shot).decode("ascii"),
        "rect": (probe or {}).get("canvas_rect"),
        "background": (probe or {}).get("page_background"),
        # The thresholds travel with the payload so the browser measures against
        # the same numbers this module judges by, rather than a second copy of
        # them inside the script.
        "dark_luma": DARK_LUMA,
    }


def _render_in_browser(page: pathlib.Path) -> dict:
    """One render, executed in the browser thread.

    Returns the raw observations -- console errors, page errors, the probe and
    the pixel measurement -- and judges nothing: the verdict is computed by
    :func:`render_problems` on the caller's side, where it always was.
    """

    outcome = _observe_in_browser(page, settle_ms=0, frames=1)
    return {
        "console_errors": outcome["console_errors"],
        "page_errors": outcome["page_errors"],
        "measured": outcome["measured"],
        "probe": outcome["probe"],
    }


#: How long a second frame waits. Motion is only visible between two moments,
#: and a page that never moved looks exactly like one whose loop never started.
ANIMATION_SETTLE_MS = 2000


_RESIZE_PROBE_JS = """
() => {
  const canvas = document.querySelector("canvas");
  return {
    innerWidth,
    innerHeight,
    canvas: canvas
      ? {
          clientWidth: canvas.clientWidth,
          clientHeight: canvas.clientHeight,
          width: canvas.width,
          height: canvas.height,
        }
      : null,
  };
}
"""


def _observe_in_browser(
    page: pathlib.Path, *, settle_ms: int, frames: int, check_resize: bool = False
) -> dict:
    """Raw observations of one page in the browser thread, judging nothing.

    The same warm browser and the same first-frame wait as every other render;
    ``frames=2`` adds the second look an animation needs. Kept separate from the
    verdict so a caller that wants pictures and a caller that wants a verdict
    share one code path through the browser.
    """

    shared = _shared_browser()
    if shared is None:
        raise RuntimeError("playwright_unavailable")
    _playwright, browser = shared
    console_errors: list[str] = []
    page_errors: list[str] = []
    probe: dict = {}
    measured: dict = {}
    shots: list[bytes] = []
    resize: dict = {}
    view = _new_page(browser)
    try:
        def on_console(message: Any) -> None:
            if message.type != "error":
                return
            text = str(message.text).strip()
            if text and text not in console_errors:
                console_errors.append(text)

        def on_page_error(error: Any) -> None:
            text = str(error).strip()
            if text and text not in page_errors:
                page_errors.append(text)

        view.on("console", on_console)
        view.on("pageerror", on_page_error)
        view.goto(page.resolve().as_uri())
        view.wait_for_timeout(RENDER_SETTLE_MS)
        probe = view.evaluate(_PAGE_PROBE_JS)
        if frames > 0:
            shots.append(view.screenshot())
        for _ in range(max(0, frames - 1)):
            view.wait_for_timeout(settle_ms)
            shots.append(view.screenshot())
        if shots:
            measured = view.evaluate(
                _PIXEL_STATS_JS, pixel_payload(probe=probe, shot=shots[0])
            )
        if check_resize:
            # A full-viewport scene promises to follow the window. Measure it
            # instead of asking a vision model to notice a resize that happened
            # before its screenshots were taken.
            rect = probe.get("canvas_rect") if isinstance(probe, dict) else None
            fills = (
                isinstance(rect, dict)
                and float(rect.get("width") or 0) >= RENDER_VIEWPORT_WIDTH * 0.9
                and float(rect.get("height") or 0) >= RENDER_VIEWPORT_HEIGHT * 0.9
            )
            if not fills:
                resize = {"ok": None, "reason": "canvas does not fill the viewport"}
            else:
                try:
                    view.set_viewport_size({"width": 1000, "height": 700})
                    view.wait_for_timeout(1000)
                    after = view.evaluate(_RESIZE_PROBE_JS)
                    canvas = after.get("canvas") if isinstance(after, dict) else None
                    ok = isinstance(canvas, dict) and (
                        abs(int(canvas.get("clientWidth") or 0) - 1000) <= 1
                        and abs(int(canvas.get("clientHeight") or 0) - 700) <= 1
                    )
                    resize = {
                        "ok": ok,
                        "target": {"width": 1000, "height": 700},
                        "after": after if isinstance(after, dict) else {},
                    }
                except Exception as error:  # noqa: BLE001 - a probe is not a verdict
                    resize = {
                        "ok": None,
                        "error": f"{type(error).__name__}: {str(error)[:200]}",
                    }
    finally:
        view.close()
    return {
        "console_errors": console_errors,
        "page_errors": page_errors,
        "measured": measured,
        "probe": probe,
        "frames": shots,
        "resize": resize,
    }


def observe_page(
    page: pathlib.Path,
    *,
    settle_ms: int = ANIMATION_SETTLE_MS,
    check_resize: bool = False,
) -> dict:
    """Observe a page with a warm browser: two frames, console, measurement.

    Returns ``{"skipped": reason}`` when this machine cannot render right now,
    exactly like :func:`render_page`, so a call site keeps the same rule: skip
    and say so rather than guess a verdict.
    """

    from .browser_bootstrap import playwright_chromium_status

    status = playwright_chromium_status()
    if not status.ready:
        return {"skipped": status.error or "chromium_unavailable"}
    try:
        return _run_in_browser_thread(
            lambda: _observe_in_browser(
                page, settle_ms=settle_ms, frames=2, check_resize=check_resize
            )
        )
    except RuntimeError:
        return {"skipped": "playwright_unavailable"}
    except Exception as error:
        return {
            "error": f"{type(error).__name__}: {str(error)[:300]}",
            "problems": [
                f"{page.name}: could not be rendered ({type(error).__name__})"
            ],
        }


def render_page(page: pathlib.Path) -> dict:
    """Load the page in Chromium and report what a viewer would actually get.

    Returns ``{"skipped": reason}`` when no provisioned browser is available.
    This never installs one: a verification command may not turn into a
    download, and a missing browser is a fact about the machine rather than a
    defect in the page. A page that cannot be loaded at all *is* a defect.
    """

    from .browser_bootstrap import playwright_chromium_status

    status = playwright_chromium_status()
    if not status.ready:
        return {"skipped": status.error or "chromium_unavailable"}
    try:
        outcome = _run_in_browser_thread(lambda: _render_in_browser(page))
    except RuntimeError:
        return {"skipped": "playwright_unavailable"}
    except Exception as error:
        return {
            "error": f"{type(error).__name__}: {str(error)[:300]}",
            "problems": [
                f"{page.name}: could not be rendered ({type(error).__name__})"
            ],
        }
    return render_problems(
        page.name,
        page_errors=outcome["page_errors"],
        console_errors=outcome["console_errors"],
        measured=outcome["measured"],
        probe=outcome["probe"],
    )


def default_pages(root: pathlib.Path) -> list[pathlib.Path]:
    """Root-level pages, whatever case their suffix uses.

    ``glob("*.html")`` is case-sensitive on POSIX and case-insensitive on Windows,
    so the same repository would be checked on one platform and silently skipped
    on the other -- and a skipped deliverable is the exact failure this check
    exists to catch.
    """

    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    return [
        path
        for path in entries
        if path.is_file() and path.suffix.lower() == ".html"
    ][:_MAX_PAGES]


def check(
    root: pathlib.Path,
    pages: Iterable[pathlib.Path] | None = None,
    *,
    render: bool = False,
) -> dict:
    selected = list(pages) if pages else default_pages(root)
    problems: list[str] = []
    rendered: dict[str, dict] = {}
    for page in selected:
        problems.extend(check_page(page))
        if render:
            outcome = render_page(page)
            rendered[page.name] = outcome
            problems.extend(outcome.get("problems") or [])
    verdict: dict[str, Any] = {
        "ok": not problems,
        "problems": problems,
        "checked": [page.name for page in selected],
    }
    if not selected:
        # A green line over an empty scope is the trap this check was written to
        # close: one measured run opened by running it on a repository with no
        # page yet, saw it pass, and made thirty edits before checking again. The
        # verdict stays ok -- a repository without a page is not an error -- but it
        # says plainly that it proved nothing.
        verdict["vacuous"] = True
        verdict["note"] = (
            "no page in this repository to check, so this pass proves nothing "
            "about a deliverable; run it again once the page exists"
        )
    if render:
        verdict["render"] = rendered
    return verdict


def shadowed_by_repository() -> Optional[str]:
    """Report the path that shadows this module, or ``None`` when it is genuine.

    ``python -m karox.web_check`` puts the working directory first on ``sys.path``,
    and a check runs in the repository it checks. A measured run wrote
    ``karox/web_check.py`` into the repository, so *that* file answered the check,
    passed it, and KaroX reported the page verified on the strength of a checker
    the deliverable had written. A check the artifact can rewrite is not a check,
    so this one refuses to be that: it looks up ``karox.web_check`` with the
    working directory removed from the path and compares the origin with itself.

    ``None`` also covers the case where nothing else provides the module -- a
    checkout of KaroX itself, where the repository *is* the source of truth.
    """

    import importlib.util

    import sys

    here = pathlib.Path(__file__).resolve()
    cwd = pathlib.Path.cwd().resolve()
    stripped = [
        entry
        for entry in sys.path
        if entry and not _same_place(pathlib.Path(entry), cwd)
    ]
    saved = list(sys.path)
    try:
        sys.path = stripped
        spec = importlib.util.find_spec("karox.web_check")
    except Exception:
        return None
    finally:
        sys.path = saved
    origin = getattr(spec, "origin", None) if spec is not None else None
    if not isinstance(origin, str) or not origin:
        return None
    try:
        other = pathlib.Path(origin).resolve()
    except OSError:
        return None
    if other == here:
        return None
    return str(other)


def _same_place(candidate: pathlib.Path, other: pathlib.Path) -> bool:
    try:
        return candidate.resolve() == other
    except OSError:
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="check that a page's modules resolve and, optionally, renders"
    )
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--render",
        action="store_true",
        help=(
            "also load each page in the installed Chromium, report page/console "
            "errors, and fail a frame that painted nothing"
        ),
    )
    parser.add_argument("pages", nargs="*")
    args = parser.parse_args(argv)
    root = pathlib.Path(args.root)
    shadow = shadowed_by_repository()
    if shadow is not None:
        # Refusing is the only honest answer: this process is not KaroX's checker,
        # and a verdict from it would certify the artifact against itself.
        print(
            json.dumps(
                {
                    "ok": False,
                    "problems": [
                        "this repository shadows karox.web_check with "
                        f"{shadow}, so the check that ran was not KaroX's; remove "
                        "it or run the check without the working directory on the "
                        "module path"
                    ],
                    "shadowed_by": shadow,
                },
                ensure_ascii=False,
            )
        )
        return 2
    pages = [root / pathlib.Path(page) for page in args.pages] if args.pages else None
    verdict = check(root, pages, render=bool(args.render))
    # KaroX hands its own checker a one-time value and requires it back: a
    # repository that shadows this module cannot echo what it was never given, so
    # its verdict is refused rather than counted as evidence.
    challenge = os.environ.get("KAROX_CHECK_CHALLENGE")
    if challenge:
        verdict["challenge"] = challenge
    verdict["checked_by"] = "karox.web_check"
    print(json.dumps(verdict, ensure_ascii=False))
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
