"""Scene census: what a three.js page actually draws, for a model that cannot see.

The aquarium run of 2026-11-12 (DeepSeek V4.1 Flash, no image input) spent 40 of
its 66 model requests sampling pixels with hand-written ``evaluate`` probes --
green counts, warm-pixel windows, colour buckets -- and still shipped plants that
rendered as scattered shards (an index list never passed to ``setIndex``). A
text-only model cannot look at a frame; it can read a table.

The census gives it that table in one call. KaroX appends a few lines to the
unminified ``three.module.js`` it serves to a local page (the import already
flows through KaroX, see ``code_sources``), so the page's own renderer reports
the scene and camera it last drew to the screen. ``census`` then renders that
scene once more into an off-screen target with every top-level object in its
own flat ID colour (no lights, shadows, fog or tone mapping), reads the pixels
back and reports, per object: the share of the frame it covers, where it sits on
screen, how many disconnected pieces it breaks into, its size in world units,
and -- for an object that covers nothing -- whether it is hidden behind others
or outside the camera. Everything the page had (materials, instance colours,
background, clear colour, shadow updates, render target) is restored before the
call returns, so the next frame is the page's own.

The census is a measurement, not a verdict: an object made of many meshes or
instances is expected to have many pieces, and the result says how many meshes
and instances each object has so the reader can compare.
"""

from __future__ import annotations

import re

#: Appended to an unminified three.js module build, where module scope still has
#: the class bindings. Every release's ``WebGLRenderer.render`` calls
#: ``scene.onBeforeRender(renderer, scene, camera, renderTarget)`` -- r160 builds
#: the renderer as a function whose ``render`` is an instance property, so a
#: prototype patch would miss it; the Scene hook works for both shapes. Only a
#: frame drawn to the screen (no render target) is remembered.
HOOK_JS = """
;(() => {
  if (typeof Scene !== 'function' || Scene.prototype.__karoxCensus) return;
  const before = Scene.prototype.onBeforeRender;
  Scene.prototype.onBeforeRender = function (renderer, scene, camera, target) {
    if (renderer && camera && !target && !(renderer.getRenderTarget && renderer.getRenderTarget())) {
      globalThis.__karoxThree = { renderer, scene: this, camera };
    } else if (renderer && camera && this.children && this.children.length) {
      // Post-processing (EffectComposer) draws the scene into a buffer and
      // only a full-screen quad reaches the screen: keep the scene as a fallback.
      globalThis.__karoxThreeOffscreen = { renderer, scene: this, camera };
    }
    return before.apply(this, arguments);
  };
  Scene.prototype.__karoxCensus = true;
  globalThis.__karoxTHREE = { MeshBasicMaterial, WebGLRenderTarget, Box3, Vector2, Vector3, Color,
    LinearSRGBColorSpace, DoubleSide, FrontSide, BackSide };
})();
"""

# r171+ moved the classes (Scene included) into three.core.js, which
# three.module.js imports; the hook goes wherever ``class Scene`` lives.
_THREE_MODULE = re.compile(r"/three(?:\.module|\.core)?\.js(?:[?#].*)?$", re.IGNORECASE)


def wants(url: str) -> bool:
    """Whether this URL names a three.js module build worth instrumenting."""

    return bool(_THREE_MODULE.search(str(url or "")))


def instrument(url: str, body: bytes) -> bytes:
    """The module with the census hook appended, or the bytes unchanged."""

    if not wants(url) or b"class Scene extends Object3D" not in body or b"__karoxCensus" in body:
        return body
    return body + HOOK_JS.encode("utf-8")


#: Runs in the page. ``opts.width`` is the census frame width in pixels.
CENSUS_JS = r"""(opts) => {
  const H = globalThis.__karoxThree || globalThis.__karoxThreeOffscreen, T = globalThis.__karoxTHREE;
  if (!H || !T) {
    return { error: 'no three.js frame seen: census needs the page to import an unminified three.module.js (or three.core.js) and render to the screen at least once' };
  }
  const { renderer, scene, camera } = H;
  const drawable = (o) => o.isMesh && o.visible;
  const mats = (m) => Array.isArray(m.material) ? m.material : [m.material];
  const seeThrough = (m) => mats(m).every((x) => x && (((x.transmission || 0) > 0.3) || (x.transparent && x.opacity < 0.6)));
  const label = (o, i) => o.name || (o.type + '#' + i + (o.children && o.children[0] && o.children[0].name ? ' (' + o.children[0].name + ')' : ''));
  const collect = (level) => {
    const out = [];
    level.forEach((o, i) => {
      const meshes = [];
      o.traverseVisible((m) => { if (drawable(m)) meshes.push(m); });
      if (!meshes.length || out.length >= 60) return;
      const instances = meshes.reduce((n, m) => n + (m.isInstancedMesh ? m.count : 1), 0);
      out.push({ obj: o, meshes, instances, label: label(o, i), see_through: meshes.every(seeThrough) });
    });
    return out;
  };
  // A page that wraps the whole scene in one group (lights beside it carry no
  // meshes) would report one row; open such a wrapper until the parts show.
  let entities = collect(scene.children.filter((o) => o.visible));
  for (let depth = 0; depth < 4 && entities.length === 1 && entities[0].obj.children.length > 1; depth++) {
    const inner = collect(entities[0].obj.children.filter((o) => o.visible));
    if (inner.length < 2) break;
    entities = inner;
  }
  const owner = new Map();
  entities.forEach((e, k) => e.meshes.forEach((m) => owner.set(m, k)));
  const W = Math.max(120, Math.min(960, Math.round(opts.width || 960)));
  const size = renderer.getSize(new T.Vector2());
  const Hh = Math.max(60, Math.round(W * (size.y || 1) / (size.x || 1)));
  const target = new T.WebGLRenderTarget(W, Hh);
  const cache = new Map();
  const material = (id, side) => {  // id 1..255, flat and unlit, exact in a linear target
    const key = id + ':' + side;
    if (!cache.has(key)) {
      const m = new T.MeshBasicMaterial({ fog: false, side, toneMapped: false });
      m.color.setRGB((id % 64) * 4 / 255, (Math.floor(id / 64) * 64 + 32) / 255, 160 / 255, T.LinearSRGBColorSpace);
      cache.set(key, m);
    }
    return cache.get(key);
  };
  const decode = (r, g, b) => (b < 140 || b > 180) ? 0 : (Math.round(r / 4) + Math.floor(g / 64) * 64);
  const saved = [];
  const state = {
    target: renderer.getRenderTarget(), background: scene.background, override: scene.overrideMaterial,
    clear: renderer.getClearColor(new T.Color()), alpha: renderer.getClearAlpha(),
    shadows: renderer.shadowMap.autoUpdate,
  };
  scene.traverse((m) => { if (m.isMesh || m.isPoints || m.isLine || m.isSprite) saved.push([m, m.material, m.instanceColor || null, m.visible]); });
  const buffer = new Uint8Array(W * Hh * 4);
  let lastIds = null;
  // Render once with idOf(mesh) -> 1..255 (0 hides it) and label every pixel region.
  const pass = (idOf) => {
    for (const [m] of saved) {
      const id = owner.has(m) ? idOf(m) : 0;
      if (!id) { m.visible = false; continue; }
      m.material = material(id, mats(m)[0] ? mats(m)[0].side : T.FrontSide);
      if (m.isInstancedMesh) m.instanceColor = null;
    }
    renderer.setRenderTarget(target);
    renderer.setClearColor(0x000000, 0);
    renderer.clear();
    renderer.render(scene, camera);
    renderer.readRenderTargetPixels(target, 0, 0, W, Hh, buffer);
    for (const [m, mat, ic, vis] of saved) { m.material = mat; m.instanceColor = ic; m.visible = vis; }
    const ids = new Uint8Array(W * Hh);
    for (let p = 0; p < W * Hh; p++) ids[p] = buffer[p * 4 + 3] ? decode(buffer[p * 4], buffer[p * 4 + 1], buffer[p * 4 + 2]) : 0;
    lastIds = ids;
    const stats = new Map(), seen = new Uint8Array(W * Hh);
    for (let p = 0; p < W * Hh; p++) {
      const id = ids[p];
      if (!id || seen[p]) continue;
      if (!stats.has(id)) stats.set(id, { count: 0, sizes: [], x0: W, y0: Hh, x1: -1, y1: -1 });
      const s = stats.get(id);
      let n = 0; const stack = [p]; seen[p] = 1;
      while (stack.length) {
        const q = stack.pop(); n++;
        const qx = q % W, qy = Hh - 1 - Math.floor(q / W);
        if (qx < s.x0) s.x0 = qx; if (qx > s.x1) s.x1 = qx; if (qy < s.y0) s.y0 = qy; if (qy > s.y1) s.y1 = qy;
        for (let dy = -W; dy <= W; dy += W) {  // 8-connected: a thin diagonal stroke stays one region
          for (let dx = qx > 0 ? -1 : 0; dx <= (qx < W - 1 ? 1 : 0); dx++) {
            const r = q + dy + dx;
            if (r >= 0 && r < W * Hh && !seen[r] && ids[r] === id) { seen[r] = 1; stack.push(r); }
          }
        }
      }
      s.count += n; s.sizes.push(n);
    }
    return stats;
  };
  const summary = (s) => {
    if (!s) return { count: 0, pieces: 0, specks: 0, largest: 0, box: null };
    const pieces = s.sizes.filter((n) => n >= 3).length;
    return { count: s.count, pieces, specks: s.sizes.length - pieces, largest: Math.max(...s.sizes),
      box: [s.x0, s.y0, s.x1, s.y1].map((v, i) => Math.round(100 * v / (i % 2 ? Hh : W))) };
  };
  // The frame as the page draws it (its materials, lights, tone mapping and
  // colour space), read straight after drawing: reading the canvas later with
  // drawImage/toDataURL returns black unless the page kept its drawing buffer.
  let shot = null;
  try {
    renderer.setRenderTarget(null);
    renderer.render(scene, camera);
    const gl = renderer.getContext();
    const bw = gl.drawingBufferWidth, bh = gl.drawingBufferHeight;
    const px = new Uint8Array(bw * bh * 4);
    gl.readPixels(0, 0, bw, bh, gl.RGBA, gl.UNSIGNED_BYTE, px);
    shot = { bw, bh, px };
  } catch (err) { shot = null; }
  const hex = (r, g, b) => '#' + [r, g, b].map((v) => Math.round(v).toString(16).padStart(2, '0')).join('');
  let rows = [], look = null;
  try {
    scene.background = null;
    scene.overrideMaterial = null;
    renderer.shadowMap.autoUpdate = false;
    // Glass and water never hide what is behind them here.
    const together = pass((m) => seeThrough(m) ? 0 : owner.get(m) + 1);
    const colours = new Map();
    if (shot) {
      const ids = lastIds, acc = new Map();
      let lum = 0, dark = 0, bright = 0, clear = 0;
      for (let y = 0; y < Hh; y++) {
        const sy = Math.min(shot.bh - 1, Math.floor((y + 0.5) * shot.bh / Hh));
        for (let x = 0; x < W; x++) {
          const sx = Math.min(shot.bw - 1, Math.floor((x + 0.5) * shot.bw / W));
          const q = (sy * shot.bw + sx) * 4, alpha = shot.px[q + 3];
          // A transparent canvas shows the page behind it there (its CSS background).
          if (alpha < 8) { clear++; continue; }
          const un = alpha < 255 ? 255 / alpha : 1;  // the buffer is premultiplied
          const r = Math.min(255, shot.px[q] * un), g = Math.min(255, shot.px[q + 1] * un), b = Math.min(255, shot.px[q + 2] * un);
          const l = 0.2126 * r + 0.7152 * g + 0.0722 * b;
          lum += l; if (l < 20) dark++; if (l > 240) bright++;
          const id = ids[y * W + x];
          if (!acc.has(id)) acc.set(id, { n: 0, lum: 0, bins: new Map() });
          const a = acc.get(id), key = ((r | 0) >> 5) * 64 + ((g | 0) >> 5) * 8 + ((b | 0) >> 5);
          a.n++; a.lum += l;
          if (!a.bins.has(key)) a.bins.set(key, [0, 0, 0, 0]);
          const bin = a.bins.get(key); bin[0] += r; bin[1] += g; bin[2] += b; bin[3]++;
        }
      }
      // Its main colours (a striped fish is two colours, not their average).
      acc.forEach((a, id) => colours.set(id, { brightness: Math.round(a.lum / a.n),
        colours: [...a.bins.values()].sort((u, v) => v[3] - u[3]).filter((v, i) => i === 0 || v[3] >= 0.15 * a.n).slice(0, 3)
          .map((v) => [hex(v[0] / v[3], v[1] / v[3], v[2] / v[3]), Math.round(100 * v[3] / a.n)]) }));
      const n = W * Hh, drawn = Math.max(1, n - clear), bg = colours.get(0);
      look = { mean_brightness: Math.round(lum / drawn), dark_pct: +(100 * dark / drawn).toFixed(1), blown_out_pct: +(100 * bright / drawn).toFixed(1),
        background: bg ? bg.colours : null };
      if (clear) look.transparent_pct = +(100 * clear / n).toFixed(1);
    }
    rows = entities.map((e, k) => {
      const seen = summary(together.get(k + 1));
      let solo = null, regions = null;
      const own = new Map(e.meshes.slice(0, 255).map((m, i) => [m, i + 1]));
      if (!seen.count || e.see_through) {
        // The object alone: its own coverage and box with nothing in front.
        const alone = pass((m) => (own.has(m) && (e.see_through || !seeThrough(m))) ? own.get(m) : 0);
        let count = 0, x0 = 101, y0 = 101, x1 = -1, y1 = -1;
        alone.forEach((s) => {
          count += s.count;
          const b = summary(s).box;
          x0 = Math.min(x0, b[0]); y0 = Math.min(y0, b[1]); x1 = Math.max(x1, b[2]); y1 = Math.max(y1, b[3]);
        });
        solo = { count, box: count ? [x0, y0, x1, y1] : null };
      }
      // How many regions one mesh draws as with nothing else drawn, not even
      // its siblings (pebbles on a bed would cut the bed into pieces). One
      // flat-coloured mesh is a cheap draw, so a sample of up to 8 is enough.
      const solid = e.meshes.filter((m) => e.see_through || !seeThrough(m));
      const sample = solid.length <= 8 ? solid : Array.from({ length: 8 }, (_, i) => solid[Math.floor(i * solid.length / 8)]);
      regions = [];
      for (const target of sample) {
        const s = pass((m) => m === target ? 1 : 0).get(1);
        // An instanced mesh draws one region per instance by design.
        const copies = target.isInstancedMesh ? Math.max(1, target.count) : 1;
        if (s && s.count >= 12) regions.push(Math.max(1, Math.ceil(s.sizes.length / copies)));
      }
      const box = new T.Box3().setFromObject(e.obj);
      const dims = box.isEmpty() ? null : box.getSize(new T.Vector3()).toArray().map((v) => +v.toFixed(2));
      const centre = box.isEmpty() ? null : box.getCenter(new T.Vector3()).toArray().map((v) => +v.toFixed(2));
      const shownCount = e.see_through ? solo.count : seen.count;
      const row = { object: e.label, meshes: e.meshes.length, instances: e.instances,
        frame_pct: +(100 * shownCount / (W * Hh)).toFixed(2), screen_box_pct: e.see_through ? solo.box : seen.box,
        pieces: seen.pieces, largest_piece_pct: seen.count ? Math.round(100 * seen.largest / seen.count) : 0,
        world_size: dims, world_centre: centre };
      if (e.see_through) row.see_through = true;
      const c = colours.get(k + 1);
      if (c) { row.colours = c.colours; row.brightness = c.brightness; }
      if (regions && regions.length) {
        regions.sort((a, b) => a - b);
        row.regions_per_mesh = regions[Math.floor(regions.length / 2)];
      }
      if (!shownCount) {
        row.note = (!e.see_through && solo && solo.count) ? 'hidden behind other objects from this camera' : 'outside the camera frame or too small to cover a pixel';
      } else if ((row.regions_per_mesh || 0) >= 4) {
        row.note = 'one of its meshes draws as ' + row.regions_per_mesh + ' separate regions when drawn alone (median over ' + regions.length + ' meshes): a solid part is rendering as scattered fragments. Usual causes: vertices displaced one by one on a non-indexed geometry (Icosahedron, Dodecahedron, Octahedron, Tetrahedron and most built-ins after toNonIndexed keep one copy of each corner per face, so random jitter tears the faces apart: merge with BufferGeometryUtils.mergeVertices before displacing, or displace by a function of the position), an index list never passed to setIndex, or degenerate triangles';
      }
      return row;
    });
  } finally {
    for (const [m, mat, ic, vis] of saved) { m.material = mat; m.instanceColor = ic; m.visible = vis; }
    scene.background = state.background;
    scene.overrideMaterial = state.override;
    renderer.shadowMap.autoUpdate = state.shadows;
    renderer.setRenderTarget(state.target);
    renderer.setClearColor(state.clear, state.alpha);
    target.dispose();
    cache.forEach((m) => m.dispose());
  }
  const covered = rows.reduce((n, r) => n + (r.see_through ? 0 : r.frame_pct), 0);
  return { frame: [W, Hh], camera: { position: camera.position.toArray().map((v) => +v.toFixed(2)) },
    objects_covered_pct: +covered.toFixed(1), look, objects: rows,
    how_to_read: 'one row per top-level object (name your groups); frame_pct is the share of the frame it covers in front of the others, screen_box_pct is [left, top, right, bottom] in % of the frame, pieces counts its disconnected regions of 3+ pixels, regions_per_mesh how many regions one of its meshes draws as when shown alone (1-2 for a solid part); see-through objects (glass, water) do not hide others here; colours are its main colours as [hex, % of its visible pixels] and brightness (0-255) their mean, in the frame as the page draws it, and look gives the whole frame (dark_pct below 20 brightness, blown_out_pct above 240, both over the pixels the canvas draws; transparent_pct is where a transparent canvas lets the page background show) -- read these instead of sampling the canvas, which reads black unless the page keeps its drawing buffer' };
}"""


def census_width(payload_width: object) -> int:
    try:
        value = int(payload_width) if payload_width is not None else 960
    except (TypeError, ValueError):
        value = 960
    return max(120, min(960, value))

