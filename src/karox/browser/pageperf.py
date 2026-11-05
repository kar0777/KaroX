"""Page responsiveness signals and in-page evaluation for local pages.

A page the agent builds can pass every functional probe and still be unusable:
a WebGL scene that renders every frame at 1440x900 with soft shadows takes
0.5-1.3 s per frame in a browser without a GPU, and every click, key press and
read then waits for the main thread. Measured runs shipped such pages; their
own clicks took 7-15 s while the agent read only "clicked: true", and the
user's check (a normal click with a 4 s timeout) failed on a correct feature.
The browser therefore reports what it measured -- the frame time after a
navigation and the time an input action took -- and says what it means.

``evaluate`` reads page state in one call. Without it, an agent wrote a probe
page per question and spent an open, a wait and a read for each answer (16
model turns of one measured 3D run). It runs only on pages served from this
machine, where the agent is the page's author.
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping, Optional

#: Median animation-frame interval at or above which a page is reported slow.
#: 60 fps is 16.7 ms; 200 ms is five frames a second, where input visibly lags.
SLOW_FRAME_MS = 200
#: An input action that took longer than this is reported with its duration.
SLOW_INPUT_MS = 1500
#: How long a click or key press may wait for its target. A control that needs
#: longer is broken for a user; the old 15 s ceiling cost a measured 3D run a
#: minute of wall time in four timed-out clicks and still said nothing about why.
INPUT_TIMEOUT_SECONDS = 6.0
#: Bound on the JSON an evaluation returns to the model.
MAX_EVALUATE_CHARS = 6000
#: Bound on the expression text.
MAX_EXPRESSION_CHARS = 20_000
DEFAULT_EVALUATE_TIMEOUT_MS = 10_000

#: Up to seven animation frames, at most ~2.6 s. On an idle page this returns
#: in about 0.1 s; it is slow exactly when the page is.
FRAME_PROBE_JS = """() => new Promise((resolve) => {
  const stamps = [];
  const started = performance.now();
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    const gaps = [];
    for (let i = 1; i < stamps.length; i += 1) gaps.push(stamps[i] - stamps[i - 1]);
    gaps.sort((a, b) => a - b);
    const median = gaps.length ? gaps[Math.floor(gaps.length / 2)] : performance.now() - started;
    resolve({ frames: stamps.length, frame_ms: Math.round(median) });
  };
  const tick = (now) => {
    stamps.push(now);
    if (stamps.length >= 7 || now - started > 2000) finish();
    else requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  setTimeout(finish, 2600);
})"""

HAS_CANVAS_JS = "() => !!document.querySelector('canvas')"

SLOW_FRAME_NOTE = (
    "each animation frame of this page takes about {ms} ms in this browser "
    "(smooth is 16-33 ms). While frames are this slow, clicks and key presses "
    "wait for the main thread: controls answer late and checks with normal "
    "timeouts fail. Render only while something changes (input, camera "
    "damping, an animation or a state change), cap the pixel ratio at 1, and "
    "keep antialiasing, shadow-map size and geometry modest; then open the "
    "page again to confirm."
)

SLOW_INPUT_NOTE = (
    "this {action} took {seconds:.1f} s; a responsive control answers well "
    "under a second. The page's main thread is busy (slow frames or heavy work), "
    "so users and checks with normal timeouts see an unresponsive control."
)

INPUT_TIMEOUT_NOTE = (
    "the {action} did not complete in time. This page's frames take about "
    "{ms} ms here, so input waits for the main thread; make frames cheaper (see "
    "the render note from open) and try again. "
)

_EXPRESSION_TEMPLATE = """async () => {
  const __limit = %d;
  const __timer = new Promise((_, reject) => setTimeout(
    () => reject(new Error('evaluate timed out after ' + __limit + ' ms')), __limit));
  const __run = async () => {
    let __value = (
%s
    );
    if (typeof __value === 'function') __value = __value();
    return await __value;
  };
  return await Promise.race([__run(), __timer]);
}"""

_BODY_TEMPLATE = """async () => {
  const __limit = %d;
  const __timer = new Promise((_, reject) => setTimeout(
    () => reject(new Error('evaluate timed out after ' + __limit + ' ms')), __limit));
  const __run = async () => {
%s
  };
  return await Promise.race([__run(), __timer]);
}"""


def frame_probe_enabled() -> bool:
    return os.environ.get("KAROX_BROWSER_FRAME_PROBE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def expression_source(expression: str, timeout_ms: int) -> str:
    """The function Playwright calls for one expression (or arrow function)."""

    text = expression.strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    return _EXPRESSION_TEMPLATE % (int(timeout_ms), text)


def body_source(body: str, timeout_ms: int) -> str:
    """The function Playwright calls for statements that ``return`` a value."""

    return _BODY_TEMPLATE % (int(timeout_ms), body)


def is_syntax_error(exc: BaseException) -> bool:
    return "SyntaxError" in str(exc)


def evaluate_timeout_ms(payload: Mapping[str, Any], deadline_seconds: float) -> int:
    raw = payload.get("timeout_ms")
    try:
        requested = int(raw) if raw is not None else DEFAULT_EVALUATE_TIMEOUT_MS
    except (TypeError, ValueError):
        requested = DEFAULT_EVALUATE_TIMEOUT_MS
    ceiling = max(500, int(min(float(deadline_seconds), 30.0) * 1000) - 500)
    return max(250, min(requested, ceiling))


def bounded_value(value: Any) -> dict[str, Any]:
    """Return an evaluation result that fits the model's context."""

    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = json.dumps(str(value), ensure_ascii=False)
    if len(text) <= MAX_EVALUATE_CHARS:
        return {"value": value}
    return {"value_text": text[:MAX_EVALUATE_CHARS], "truncated": True, "chars": len(text)}


def slow_frame_report(data: Any) -> Optional[dict[str, Any]]:
    """The render note for a frame probe result, or None when frames are fine."""

    if not isinstance(data, Mapping):
        return None
    try:
        frame_ms = int(data.get("frame_ms") or 0)
    except (TypeError, ValueError):
        return None
    if frame_ms < SLOW_FRAME_MS:
        return None
    return {"frame_ms": frame_ms, "slow": True, "note": SLOW_FRAME_NOTE.format(ms=frame_ms)}


def slow_input_note(action: str, seconds: float) -> Optional[dict[str, Any]]:
    if seconds * 1000 < SLOW_INPUT_MS:
        return None
    return {
        "elapsed_ms": int(seconds * 1000),
        "slow": SLOW_INPUT_NOTE.format(action=action, seconds=seconds),
    }
