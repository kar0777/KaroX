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

#: The frames right after an input: up to eight, at most ~2.6 s. Returns the
#: gaps themselves -- an answer of three slow frames followed by idle ones has
#: a fast median, and those three frames are what the next input waits for.
RESPONSE_FRAME_PROBE_JS = """() => new Promise((resolve) => {
  const stamps = [];
  const started = performance.now();
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    const gaps = [];
    for (let i = 1; i < stamps.length; i += 1) gaps.push(Math.round(stamps[i] - stamps[i - 1]));
    resolve({ frames: stamps.length, gaps, waited_ms: Math.round(performance.now() - started) });
  };
  const tick = (now) => {
    stamps.push(now);
    if (stamps.length >= 8 || now - started > 2200) finish();
    else requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  setTimeout(finish, 2600);
})"""


#: What makes one frame expensive where WebGL runs on the CPU. A measured
#: 3d-viewer run (f1) followed the older advice -- draw only on change, pixel
#: ratio 1, no antialiasing -- kept an environment map (PMREM) on standard
#: materials, and still drew 0.7 s frames: its idle probes read fast, a drag
#: took 100.8 s, and the user's clicks after an orbit timed out. The DeepSeek
#: Harness page of the same round also lit standard materials with a PMREM
#: environment and failed the same four clicks; the passing scenes measured
#: since r2b had none. The model also timed ``renderer.render()`` inside
#: evaluate, read 0 ms for every variant (the call only queues the work) and
#: concluded nothing was slow.
FRAME_COST_ADVICE = (
    "Without a GPU the costly parts are environment maps (PMREM, "
    "scene.environment), standard and physical materials (Lambert or Phong "
    "cost far less), real-time shadows, transparent layers, post-processing, "
    "antialiasing and a pixel ratio above 1. Timing a draw call inside "
    "evaluate shows only how fast the work was queued; the frame time "
    "measured here is the real cost."
)

SLOW_FRAME_NOTE = (
    "each animation frame of this page takes about {ms} ms in this browser "
    "(smooth is 16-33 ms). While frames are this slow, clicks and key presses "
    "wait for the main thread: controls answer late and checks with normal "
    "timeouts fail. Make one frame cheaper: drawing only on change hides the "
    "cost while the page is idle, not during a drag, camera damping or a "
    "tween, which draw every frame. " + FRAME_COST_ADVICE + " Then drag or "
    "click once: the result reports the frames the page drew in response."
)

#: A page that once drew slow frames and now reads fast may only have stopped
#: drawing while idle; the f1 run reloaded eight times after that change and
#: never saw the 0.7 s frames again until a drag took 100.8 s. The reading is
#: stated, not judged: "fast" was printed for 167-183 ms frames (f3 3d-viewer),
#: which are five or six frames a second.
IDLE_AFTER_SLOW_NOTE = (
    "frames read {ms} ms now (smooth is 16-33 ms), but this page drew frames "
    "of about {earlier} ms before. If it now draws only on change, a drag, camera "
    "damping or a tween still pays that on every frame: drag or click once "
    "and read the frames it reports (render_after)."
)

#: The frames a page draws while it answers an input. The input itself can
#: return fast while its answer -- a camera tween, damping after a drag -- keeps
#: the main thread busy, and the next click or key press waits behind it.
RESPONSE_FRAME_NOTE = (
    "after this {action} the page drew frames of about {ms} ms each (smooth is "
    "16-33 ms). Input that arrives meanwhile -- the next click, key press or "
    "drag step -- waits for them, so checks with normal timeouts fail even "
    "when this {action} returned quickly. Drawing only on change does not "
    "help while the page answers: make one frame cheaper. " + FRAME_COST_ADVICE
)

SLOW_INPUT_NOTE = (
    "this {action} took {seconds:.1f} s; a responsive control answers well "
    "under a second. The page's main thread is busy (slow frames or heavy work), "
    "so users and checks with normal timeouts see an unresponsive control."
)

#: Playwright acts only on an element that holds still for two animation
#: frames. A control that keeps drifting -- an orbit camera's leftover damping
#: momentum after a programmatic reset, a tween that never settles, an endless
#: CSS animation -- makes every click wait, and a check with a short timeout
#: fails on a feature that works. The old note blamed a busy main thread for
#: that too (3d-viewer, r3), so the model optimised frames instead of stopping
#: the motion.
MOVING_TARGET_NOTE = (
    "this {action} took {seconds:.1f} s because its target kept moving: it "
    "shifted {px} px while the {action} waited for it to hold still. Clicks "
    "wait for a still element, so users' checks with normal timeouts fail on "
    "it. Make the motion end: let camera damping or a tween settle, clear "
    "leftover momentum when the view is set by code (reset, focus), and give "
    "CSS animations an end."
)

MOVING_TIMEOUT_NOTE = (
    "the {action} timed out because its target never held still (Playwright "
    "reported it 'not stable'). Something keeps moving it -- camera damping or "
    "a tween that never settles, leftover momentum after the view is set by "
    "code, an endless CSS animation. Make the motion end, then try again. "
)

#: Records the target's position every frame while an input waits for it, so
#: a slow click can say whether the element was moving. It stops by itself
#: after a short window (an input slower than SLOW_INPUT_MS has been waiting
#: through all of it); MOTION_READ_JS stops it early and reads it.
MOTION_RECORDER_JS = """el => {
  const previous = window.__karoxMotion;
  if (previous) previous.stop = true;
  const record = { boxes: [], stop: false };
  window.__karoxMotion = record;
  const started = performance.now();
  const tick = () => {
    if (record.stop || record.boxes.length >= 180 || performance.now() - started > 2500) return;
    const r = el.getBoundingClientRect();
    record.boxes.push([r.x, r.y]);
    requestAnimationFrame(tick);
  };
  tick();
  return true;
}"""

MOTION_READ_JS = """() => {
  const record = window.__karoxMotion;
  if (!record) return null;
  record.stop = true;
  window.__karoxMotion = null;
  const boxes = record.boxes;
  let moved = 0;
  let moving = 0;
  for (let i = 1; i < boxes.length; i++) {
    const step = Math.max(Math.abs(boxes[i][0] - boxes[i - 1][0]), Math.abs(boxes[i][1] - boxes[i - 1][1]));
    if (step > 0.01) moving += 1;
    moved = Math.max(moved, Math.abs(boxes[i][0] - boxes[0][0]), Math.abs(boxes[i][1] - boxes[0][1]));
  }
  return { frames: boxes.length, moving_frames: moving, moved_px: Math.round(moved * 10) / 10 };
}"""

#: Below this the box only jittered by sub-pixel rounding.
MOVING_TARGET_PX = 1.0

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


def idle_after_slow_report(frame_ms: int, earlier_ms: int) -> dict[str, Any]:
    """The note for a fast probe on a page that drew slow frames before."""

    return {
        "frame_ms": int(frame_ms),
        "earlier_frame_ms": int(earlier_ms),
        "note": IDLE_AFTER_SLOW_NOTE.format(ms=int(frame_ms), earlier=int(earlier_ms)),
    }


def response_frame_report(data: Any, action: str) -> Optional[dict[str, Any]]:
    """The note for the frames a page drew after an input, or None when fine.

    Two slow gaps make an answer slow; so does a window in which not even a
    second frame arrived (one frame longer than the whole probe).
    """

    if not isinstance(data, Mapping):
        return None
    raw = data.get("gaps")
    gaps = [
        int(gap)
        for gap in (raw if isinstance(raw, (list, tuple)) else [])
        if isinstance(gap, (int, float)) and not isinstance(gap, bool)
    ]
    slow = sorted(gap for gap in gaps if gap >= SLOW_FRAME_MS)
    if not gaps:
        try:
            waited = int(data.get("waited_ms") or 0)
        except (TypeError, ValueError):
            return None
        if waited < 2 * SLOW_FRAME_MS:
            return None
        slow = [waited]
    elif len(slow) < 2:
        return None
    ms = slow[len(slow) // 2]
    return {
        "frame_ms": ms,
        "slow_frames": len(slow),
        "note": RESPONSE_FRAME_NOTE.format(action=action, ms=ms),
    }


def slow_input_note(
    action: str, seconds: float, motion: Optional[Mapping[str, Any]] = None
) -> Optional[dict[str, Any]]:
    """Name a slow input and, when it was measured, the reason.

    ``motion`` is the MOTION_READ_JS reading recorded while the input waited:
    a moving target explains the wait better than a busy main thread does.
    """

    if seconds * 1000 < SLOW_INPUT_MS:
        return None
    moved = target_moved(motion)
    if moved is not None:
        return {
            "elapsed_ms": int(seconds * 1000),
            "slow": MOVING_TARGET_NOTE.format(action=action, seconds=seconds, px=moved[0]),
        }
    return {
        "elapsed_ms": int(seconds * 1000),
        "slow": SLOW_INPUT_NOTE.format(action=action, seconds=seconds),
    }


def target_moved(motion: Optional[Mapping[str, Any]]) -> Optional[tuple[float, int]]:
    """(pixels, moving frames) when a MOTION_READ_JS reading shows real movement."""

    if not isinstance(motion, Mapping):
        return None
    try:
        px = float(motion.get("moved_px") or 0)
        moving = int(motion.get("moving_frames") or 0)
    except (TypeError, ValueError):
        return None
    if px < MOVING_TARGET_PX or moving < 2:
        return None
    return px, moving


def unstable_target_timeout(exc: BaseException) -> bool:
    """Playwright's call log says the target never held still."""

    text = str(exc)
    return "Timeout" in text and "not stable" in text
