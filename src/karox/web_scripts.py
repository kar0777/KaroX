"""JavaScript snippets used by web checks (render/observe/probe)."""

from __future__ import annotations

import re


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
  // Namespaced so the same measurement works inside an SVG document, where
  // createElement("canvas") makes an element without a drawing context.
  const canvas = document.createElementNS("http://www.w3.org/1999/xhtml", "canvas");
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
  const tone_sums = {};
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
  let bottom_structured = 0;
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
      const sum = tone_sums[tone] || (tone_sums[tone] = [0, 0, 0]);
      sum[0] += data[here]; sum[1] += data[here + 1]; sum[2] += data[here + 2];
      const luma = (0.2126 * data[here] + 0.7152 * data[here + 1] +
                    0.0722 * data[here + 2]) / 255;
      luma_sum += luma;
      luma_square_sum += luma * luma;
      if (y >= bottom_edge) {
        bottom_sampled += 1;
        if (delta > 24) {
          bottom_structured += 1;
        }
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
  // The mean colour of the modal bucket. Decoding the 3-bit bucket with the
  // 5-bit tone layout reported white as "56,56,56" -- a dark grey the frame
  // never held -- so the colour is measured, not reconstructed.
  const modal_color = modal_tone === null ? null : tone_sums[modal_tone]
    .map((total) => Math.round(total / modal_count)).join(",");
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
    bottom_structured_share: bottom_sampled
      ? Number((bottom_structured / bottom_sampled).toFixed(4))
      : null,
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
  const host = document.body || document.documentElement;
  const background =
    (host ? getComputedStyle(host).backgroundColor : "") ||
    (document.documentElement
      ? getComputedStyle(document.documentElement).backgroundColor
      : "") || "";
  const dynamic_dom = Boolean(
    document.querySelector("script,canvas,video,audio,iframe,object,embed,link,img,picture,source,image,animate,animateTransform,animateMotion,set") ||
    (typeof document.getAnimations === "function" && document.getAnimations().length > 0)
  );
  return {
    canvases: document.querySelectorAll("canvas").length,
    canvas_rect: rect,
    paints_with_three,
    page_background: background,
    dynamic_dom,
  };
}
"""

#: A software-rendered WebGL scene (SwiftShader: no GPU in the sandbox, and
#: ``--disable-gpu`` everywhere) draws one frame every 3-6 s. Its animation
#: loop then owns the page's main thread, and every evaluate and screenshot
#: of an observation waited behind whole frames: the b0 aquarium page took
#: 97 s per look (probe 26 s, first screenshot 24 s, second 16 s, pixel
#: statistics 8 s, resize 12 s) and the visual check spent 405 s of a 1022 s
#: run on four looks. The gate holds ``requestAnimationFrame`` callbacks while
#: KaroX measures and photographs, and releases them in between, so the page
#: draws only when its frames are wanted and the reads run on an idle thread.
#: It counts drawn frames, so the second picture is taken after the page
#: really drew again -- not after two seconds in which a slow page drew
#: nothing -- and it counts pending requests, so a static page is never
#: waited on.
_FRAME_GATE_JS = """
(() => {
  if (window.__karoxFrameGate) return;
  const native = window.requestAnimationFrame.bind(window);
  const cancelNative = window.cancelAnimationFrame.bind(window);
  const gate = { paused: false, frames: 0, pending: 0, seq: 1 << 30,
                 queue: [], live: new Map() };
  const schedule = (id, cb) => {
    const nativeId = native((time) => {
      gate.live.delete(id);
      gate.pending = Math.max(0, gate.pending - 1);
      gate.frames += 1;
      cb(time);
    });
    gate.live.set(id, { nativeId, cb });
  };
  window.requestAnimationFrame = function (cb) {
    const id = ++gate.seq;
    gate.pending += 1;
    if (gate.paused) gate.queue.push([id, cb]);
    else schedule(id, cb);
    return id;
  };
  window.cancelAnimationFrame = function (id) {
    const queued = gate.queue.findIndex((entry) => entry[0] === id);
    if (queued >= 0) {
      gate.queue.splice(queued, 1);
      gate.pending = Math.max(0, gate.pending - 1);
      return;
    }
    const live = gate.live.get(id);
    if (live) {
      cancelNative(live.nativeId);
      gate.live.delete(id);
      gate.pending = Math.max(0, gate.pending - 1);
      return;
    }
    cancelNative(id);
  };
  // A frame already handed to the browser would still draw after the pause,
  // and on a software-rendered scene one frame is seconds: the probe and the
  // capture then wait for it. Pausing takes those frames back as well.
  gate.pause = () => {
    gate.paused = true;
    for (const [id, entry] of gate.live) {
      cancelNative(entry.nativeId);
      gate.queue.push([id, entry.cb]);
    }
    gate.live.clear();
    return gate.frames;
  };
  gate.resume = () => {
    gate.paused = false;
    const held = gate.queue;
    gate.queue = [];
    for (const [id, cb] of held) schedule(id, cb);
    return gate.frames;
  };
  Object.defineProperty(window, "__karoxFrameGate", { value: gate });
})();
"""

#: A standalone SVG is drawn at its own width and height, so a 400x300 picture
#: arrives as a small corner of the frame and the checker judges a thumbnail.
#: With a viewBox the picture scales without distortion, so the observed copy
#: is fitted to the viewport. The file is not touched.
_SVG_FIT_JS = """
() => {
  const root = document.documentElement;
  if (document.body || !root || root.namespaceURI !== "http://www.w3.org/2000/svg") return false;
  if (!root.getAttribute("viewBox")) return false;
  root.setAttribute("width", "100%");
  root.setAttribute("height", "100%");
  return true;
}
"""

_FRAMES_DRAWN_JS = (
    "(n) => { const g = window.__karoxFrameGate;"
    " return !g || g.frames >= n || (g.pending === 0 && g.queue.length === 0); }"
)

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

_SCRATCH_PAGE = re.compile(
    r"(?:^|[-_. ])(?:probe|scratch|debug|tmp|temp)\d*(?:$|[-_. ])", re.I
)

_SCRATCH_ARTIFACT = re.compile(
    r"(?:^|[-_. ])(?:probe|scratch|tmp|temp)\d*(?:$|[-_. ])", re.I
)
