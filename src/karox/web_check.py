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

from .web_document import (  # noqa: F401
    _MAX_PICTURE_BYTES as _MAX_PICTURE_BYTES,
    check_picture as check_picture,
    _ADDONS as _ADDONS,
    _BARE_SIDE_EFFECT as _BARE_SIDE_EFFECT,
    _DYNAMIC_MARKUP as _DYNAMIC_MARKUP,
    _DocumentEndParser as _DocumentEndParser,
    _FROM as _FROM,
    _IMPORT_MAP as _IMPORT_MAP,
    _JS_DECLARATION as _JS_DECLARATION,
    _MAX_PAGES as _MAX_PAGES,
    _MODULE_BLOCK as _MODULE_BLOCK,
    _SVG_NAMESPACE as _SVG_NAMESPACE,
    _ScriptBlockParser as _ScriptBlockParser,
    _addons_without_three_mapping as _addons_without_three_mapping,
    _document_problems as _document_problems,
    _duplicate_declarations as _duplicate_declarations,
    _resolves_bare as _resolves_bare,
    _same_place as _same_place,
    _top_level_declarations as _top_level_declarations,
    check_page as check_page,
    default_pages as default_pages,
    default_pictures as default_pictures,
    import_map_targets as import_map_targets,
    is_scratch_artifact as is_scratch_artifact,
    is_scratch_page as is_scratch_page,
    is_static_document as is_static_document,
    module_specifiers as module_specifiers,
    shadowed_by_repository as shadowed_by_repository,
)

from .web_scripts import (  # noqa: F401
    _FRAMES_DRAWN_JS as _FRAMES_DRAWN_JS,
    _FRAME_GATE_JS as _FRAME_GATE_JS,
    _PAGE_PROBE_JS as _PAGE_PROBE_JS,
    _PIXEL_STATS_JS as _PIXEL_STATS_JS,
    _RESIZE_PROBE_JS as _RESIZE_PROBE_JS,
    _SCRATCH_ARTIFACT as _SCRATCH_ARTIFACT,
    _SCRATCH_PAGE as _SCRATCH_PAGE,
    _SVG_FIT_JS as _SVG_FIT_JS,
)

import argparse
import atexit
import base64
import hashlib
import json
import os
import pathlib
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, Optional, Sequence


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
#: Share of the bottom quarter that must still hold an edge before a dark bottom
#: counts as unlit. A neon runner's floor is dark by design but its grid, lane
#: lines and rails give the lower frame strong local structure. The old rule
#: saw only brightness, so it called a readable dark track a missing floor.
DARK_BOTTOM_STRUCTURE = 0.04
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
        from .browser.render_backend import launch_args, software_args

        # The configured executable is accepted only by browser_bootstrap:
        # official Playwright installation, or explicit SHA-pinned host approval.
        # Never let the repository, a PATH entry or model text choose a browser.
        from .browser_bootstrap import playwright_chromium_status

        status = playwright_chromium_status()
        if not status.ready or not status.executable:
            playwright.stop()
            return None
        browser = None
        for args in (launch_args(), software_args()):
            try:
                browser = playwright.chromium.launch(
                    args=args, executable_path=status.executable
                )
                break
            except Exception:
                continue
        if browser is None:
            try:
                playwright.stop()
            except Exception:
                pass
            return None
        _browser_state["playwright"] = playwright
        _browser_state["browser"] = browser
        # Rendering checks run beside the user's own work; below-normal
        # priority keeps a software-rendered WebGL frame from starving it.
        from .browser.resources import demote_headless_browsers

        demote_headless_browsers()
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
            "bottom_sampled", "bottom_structured_share", "dark_bottom_share", "luma_mean", "luma_std",
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
        bottom_structure = result.get("bottom_structured_share")
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
            # Brightness alone confuses an intentionally dark neon floor with a
            # missing one. If the lower frame still holds lane lines, grid or
            # rails, the floor is drawn and the dark tone is the design. Missing
            # structure is the fact that made the old rule useful; keep that.
            unlit = not isinstance(bottom_structure, float) or (
                bottom_structure <= DARK_BOTTOM_STRUCTURE
            )
            if dark_share > DARK_BOTTOM_SHARE and unlit:
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

#: How long a page may take to draw its first frames, to draw again between
#: the two frames, and to redraw after the resize. Bounds, not waits: a page
#: that draws at 60 fps passes each in one frame.
FIRST_FRAMES_TIMEOUT_MS = 30_000
MOTION_FRAMES_TIMEOUT_MS = 20_000
RESIZE_FRAMES_TIMEOUT_MS = 10_000







_FINGERPRINT_SUFFIXES = frozenset({
    ".html", ".htm", ".svg", ".css", ".js", ".mjs", ".json",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".woff", ".woff2", ".ttf",
})


def observation_fingerprint(
    page: pathlib.Path,
    *,
    serve_root: pathlib.Path | None = None,
    viewport: tuple[int, int] = (RENDER_VIEWPORT_WIDTH, RENDER_VIEWPORT_HEIGHT),
    query: str = "",
    backend: str | None = None,
) -> Optional[str]:
    """Freshness digest binding page bytes, served repository assets, viewport, query and backend."""

    try:
        if page.is_symlink():
            return None
        resolved = page.resolve()
        root = pathlib.Path(serve_root).resolve() if serve_root is not None else resolved.parent
        rel_page = resolved.relative_to(root).as_posix()
        if resolved.stat().st_size > _REFERENCE_MAX_BYTES:
            return None
        data = resolved.read_bytes()
    except (OSError, ValueError):
        return None
    if backend is None:
        from .browser.render_backend import launch_args

        backend = " ".join(launch_args())
    hasher = hashlib.sha256()
    hasher.update(f"page:{rel_page}\0vp:{viewport[0]}x{viewport[1]}\0q:{query}\0be:{backend}\0".encode("utf-8"))
    hasher.update(hashlib.sha256(data).digest())
    seen = 0
    total_bytes = 0
    try:
        for directory, subdirs, files in os.walk(root, followlinks=False):
            dir_path = pathlib.Path(directory)
            if dir_path.is_symlink():
                return None
            subdirs[:] = sorted(
                d for d in subdirs
                if d not in _REFERENCE_SKIP_DIRS and not (dir_path / d).is_symlink()
            )
            for name in sorted(files):
                seen += 1
                if seen > _REFERENCE_MAX_FILES:
                    return None
                item = dir_path / name
                if item.is_symlink():
                    return None
                if item.suffix.lower() not in _FINGERPRINT_SUFFIXES:
                    continue
                resolved_item = item.resolve()
                rel = resolved_item.relative_to(root).as_posix()
                size = resolved_item.stat().st_size
                total_bytes += size
                if total_bytes > _REFERENCE_MAX_BYTES:
                    return None
                hasher.update(f"{rel}:".encode("utf-8"))
                hasher.update(hashlib.sha256(resolved_item.read_bytes()).digest())
    except (OSError, ValueError):
        return None
    return hasher.hexdigest()


def _gate(view: Any, action: str) -> Optional[int]:
    """Pause or resume the page's frames; the frame count, or None without a gate."""

    try:
        value = view.evaluate(
            "(a) => { const g = window.__karoxFrameGate; return g ? g[a]() : null; }",
            action,
        )
    except Exception:  # noqa: BLE001 - a gate is an optimisation, never a verdict
        return None
    return value if isinstance(value, int) else None


def _frames_drawn(view: Any, count: Optional[int], timeout_ms: int) -> str:
    """Wait until ``count`` frames were drawn, or none is pending, or the bound."""

    if count is None:
        return "gate_unavailable"
    try:
        view.wait_for_function(
            _FRAMES_DRAWN_JS, arg=count, polling=100, timeout=timeout_ms
        )
        return "ready"
    except Exception:  # noqa: BLE001 - a slow page is observed as it is
        return "timed_out"




def _observe_in_browser(
    page: pathlib.Path,
    *,
    settle_ms: int,
    frames: int,
    check_resize: bool = False,
    url: str | None = None,
) -> dict:
    """Raw observations of one page in the browser thread, judging nothing."""

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
    unconditional_wait_ms = 0
    gate_calls = 0
    static_candidate = is_static_document(page)
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
        try:
            view.add_init_script(_FRAME_GATE_JS)
        except Exception:  # noqa: BLE001 - observed without the gate
            pass
        view.goto(url or page.resolve().as_uri())
        if page.suffix.lower() == ".svg":
            try:
                view.evaluate(_SVG_FIT_JS)
            except Exception:  # noqa: BLE001 - observed at its own size
                pass
        if not static_candidate:
            view.wait_for_timeout(RENDER_SETTLE_MS)
            unconditional_wait_ms += RENDER_SETTLE_MS
        gate_calls += 1
        gate_status = _frames_drawn(view, 2, FIRST_FRAMES_TIMEOUT_MS)
        drawn = _gate(view, "pause")
        if drawn is None:
            gate_status = "gate_unavailable"
        elif drawn < 2 and gate_status == "ready" and not static_candidate:
            gate_status = "still_rendering"
        try:
            probe = view.evaluate(_PAGE_PROBE_JS)
        except Exception as error:
            raise RuntimeError(f"probe_error: {type(error).__name__}: {str(error)[:200]}") from error
        dynamic_dom = bool(
            not isinstance(probe, dict)
            or probe.get("canvases")
            or probe.get("paints_with_three")
            or probe.get("dynamic_dom")
        )
        static_proven = bool(
            static_candidate
            and not dynamic_dom
            and gate_status == "ready"
            and drawn == 0
            and not page_errors
        )
        if not static_proven and static_candidate and RENDER_SETTLE_MS > 0:
            _gate(view, "resume")
            view.wait_for_timeout(RENDER_SETTLE_MS)
            unconditional_wait_ms += RENDER_SETTLE_MS
            gate_calls += 1
            gate_status = _frames_drawn(view, 2, FIRST_FRAMES_TIMEOUT_MS)
            drawn = _gate(view, "pause")
            if drawn is None:
                gate_status = "gate_unavailable"
        target_frames = 1 if static_proven and frames > 0 else frames
        if target_frames > 0:
            shots.append(view.screenshot())
        for _ in range(max(0, target_frames - 1)):
            _gate(view, "resume")
            if settle_ms > 0:
                view.wait_for_timeout(settle_ms)
                unconditional_wait_ms += settle_ms
            gate_calls += 1
            motion_status = _frames_drawn(
                view, None if drawn is None else drawn + 2, MOTION_FRAMES_TIMEOUT_MS
            )
            if motion_status == "timed_out":
                gate_status = "timed_out"
            drawn = _gate(view, "pause")
            shots.append(view.screenshot())
        if shots:
            measured = view.evaluate(
                _PIXEL_STATS_JS, pixel_payload(probe=probe, shot=shots[0])
            )
        if check_resize:
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
                    _gate(view, "resume")
                    gate_calls += 1
                    resize_status = _frames_drawn(
                        view, None if drawn is None else drawn + 1, RESIZE_FRAMES_TIMEOUT_MS
                    )
                    if resize_status != "ready":
                        view.wait_for_timeout(250)
                        unconditional_wait_ms += 250
                    _gate(view, "pause")
                    after = view.evaluate(_RESIZE_PROBE_JS)
                    canvas = after.get("canvas") if isinstance(after, dict) else None
                    ok = isinstance(canvas, dict) and (
                        abs(int(canvas.get("clientWidth") or 0) - 1000) <= 1
                        and abs(int(canvas.get("clientHeight") or 0) - 700) <= 1
                    )
                    if not ok and resize_status == "ready":
                        _gate(view, "resume")
                        view.wait_for_timeout(750)
                        unconditional_wait_ms += 750
                        _gate(view, "pause")
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
        "static_proven": static_proven,
        "gate_status": gate_status,
        "timings": {
            "unconditional_wait_ms": unconditional_wait_ms,
            "gate_calls": gate_calls,
        },
    }


def observe_page(
    page: pathlib.Path,
    *,
    settle_ms: int = ANIMATION_SETTLE_MS,
    check_resize: bool = False,
    serve_root: pathlib.Path | None = None,
) -> dict:
    """Observe a page with a warm browser: frames, console, measurement."""

    from .browser_bootstrap import playwright_chromium_status

    status = playwright_chromium_status()
    if not status.ready:
        return {"skipped": status.error or "chromium_unavailable"}
    try:
        if serve_root is not None:
            from .static_server import page_url, serve_repository

            with serve_repository(serve_root) as base:
                relative = (
                    page.resolve()
                    .relative_to(pathlib.Path(serve_root).resolve())
                    .as_posix()
                )
                url = page_url(base, serve_root, relative)
                return _run_in_browser_thread(
                    lambda: _observe_in_browser(
                        page,
                        settle_ms=settle_ms,
                        frames=2,
                        check_resize=check_resize,
                        url=url,
                    )
                )
        return _run_in_browser_thread(
            lambda: _observe_in_browser(
                page, settle_ms=settle_ms, frames=2, check_resize=check_resize
            )
        )
    except RuntimeError as error:
        if str(error) in {"playwright_unavailable", "the browser thread has been shut down"}:
            return {"skipped": "playwright_unavailable"}
        return {
            "error": f"{type(error).__name__}: {str(error)[:300]}",
            "problems": [
                f"{page.name}: could not be rendered ({type(error).__name__})"
            ],
        }
    except Exception as error:
        return {
            "error": f"{type(error).__name__}: {str(error)[:300]}",
            "problems": [
                f"{page.name}: could not be rendered ({type(error).__name__})"
            ],
        }


def render_page(page: pathlib.Path) -> dict:
    """Load the page in Chromium and report what a viewer would actually get."""

    from .browser_bootstrap import playwright_chromium_status

    status = playwright_chromium_status()
    if not status.ready:
        return {"skipped": status.error or "chromium_unavailable"}
    try:
        outcome = _run_in_browser_thread(lambda: _render_in_browser(page))
    except RuntimeError as error:
        if str(error) in {"playwright_unavailable", "the browser thread has been shut down"}:
            return {"skipped": "playwright_unavailable"}
        return {
            "error": f"{type(error).__name__}: {str(error)[:300]}",
            "problems": [
                f"{page.name}: could not be rendered ({type(error).__name__})"
            ],
        }
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










#: Files read when deciding whether a page the run created is referenced.
#: Bounded so a large tree cannot stall the completion floor; past either bound
#: nothing is marked diagnostic (the old, stricter behaviour).
_REFERENCE_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv"})
_REFERENCE_MAX_FILES = 500
_REFERENCE_MAX_BYTES = 4_000_000


def _reference_texts(root: pathlib.Path) -> Optional[dict[str, str]]:
    """Lower-cased text of the repository's files by POSIX path, or ``None``."""

    texts: dict[str, str] = {}
    try:
        for directory, subdirs, files in os.walk(root):
            subdirs[:] = sorted(d for d in subdirs if d not in _REFERENCE_SKIP_DIRS)
            for name in sorted(files):
                path = pathlib.Path(directory) / name
                if len(texts) >= _REFERENCE_MAX_FILES:
                    return None
                try:
                    if path.stat().st_size > _REFERENCE_MAX_BYTES:
                        continue
                    text = path.read_bytes().decode("utf-8", errors="ignore").lower()
                except OSError:
                    continue
                texts[path.relative_to(root).as_posix()] = text
    except OSError:
        return None
    return texts


def diagnostic_pages(
    root: pathlib.Path, created: Iterable[str], named: str = ""
) -> set[str]:
    """Pages this run created that nothing delivers, as given in ``created``.

    A run that must prove each requirement in the browser often writes its own
    harness page -- a measured 3D run wrote ``selftest.html`` driving the game's
    hooks and ``layoutprobe.html`` -- next to the real ``index.html``. Neither
    name carries a probe token, so every edit to the harness counted as a
    deliverable edit: it made all earlier evidence stale and spent the visual
    budget re-proving bytes the user never opens.

    A created page stays part of the deliverable when it is the conventional
    entry page, when ``named`` (the task text) names it, or when any other file
    of the repository refers to it by file name, directly or through another
    such page. Pages that existed before the run are never diagnostic, and when
    no other entry page exists the created pages are the deliverable. Bounded
    and fail-closed: an unreadable or very large tree marks nothing.
    """

    named_lower = str(named or "").lower()
    candidates: dict[str, str] = {}
    for item in created:
        relative = str(item).replace("\\", "/")
        name = pathlib.PurePosixPath(relative).name.lower()
        if pathlib.PurePosixPath(name).suffix not in {".html", ".htm"}:
            continue
        if name in {"index.html", "index.htm"} or is_scratch_page(name):
            continue
        if name in named_lower:
            continue
        candidates[pathlib.PurePosixPath(relative).as_posix()] = name
    if not candidates:
        return set()
    if not [page for page in default_pages(root)
            if page.relative_to(root).as_posix() not in candidates]:
        return set()
    texts = _reference_texts(root)
    if texts is None:
        return set()
    delivered = {path: text for path, text in texts.items() if path not in candidates}
    referenced: set[str] = set()
    sources = list(delivered.values())
    while sources:
        found = {
            path
            for path, name in candidates.items()
            if path not in referenced and any(name in text for text in sources)
        }
        referenced |= found
        sources = [texts.get(path, "") for path in found]
    return set(candidates) - referenced




#: A picture larger than this is not parsed: the check stays cheap and bounded.






def check(
    root: pathlib.Path,
    pages: Iterable[pathlib.Path] | None = None,
    *,
    render: bool = False,
) -> dict:
    selected = list(pages) if pages else default_pages(root)
    pictures = [] if pages else default_pictures(root)
    problems: list[str] = []
    rendered: dict[str, dict] = {}
    for page in selected:
        problems.extend(check_page(page))
        if render:
            outcome = render_page(page)
            rendered[page.name] = outcome
            problems.extend(outcome.get("problems") or [])
    for picture in pictures:
        found = check_picture(picture)
        problems.extend(found)
        if render and not found:
            # Rendered like a page, so a picture has the same browser proof:
            # loaded in Chromium, errors recorded, pixels measured.
            outcome = render_page(picture)
            rendered[picture.name] = outcome
            problems.extend(outcome.get("problems") or [])
    verdict: dict[str, Any] = {
        "ok": not problems,
        "problems": problems,
        "checked": [page.name for page in selected] + [p.name for p in pictures],
    }
    if not selected and not pictures:
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