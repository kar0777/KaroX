---
name: web-3d-scene
version: 1.7.0
description: Build or fix a three.js scene that runs in the browser.
when_to_use: 3D, three.js, WebGL, animated scenes, product viewers, particles, models in a page.
triggers: [three, webgl, canvas, "*.html"]
---
# three.js scene recipe

Scope discipline
- This is a technique guide, not a stored scene. Read the task's requirements and build what it asks.
- Do not carry over an object list, a count, a palette or a camera from another task. A recipe that only fits one prompt is a liability.
- Match the polish the brief asks for. "Realistic", "premium" or "screenshot-worthy" means every stated element is built with care (believable materials, supported fixtures, detail at the camera distance), not the smallest scene that passes a count. Keep it organized so it can be repaired surgically.

Setup
- Use the three.js version the task or repo pins. Load it the way the repo already does (import map, local file, bundler). Before using a class, confirm it exists in that version; newer helpers may be missing.
- One renderer, one scene, one camera. Set renderer size and pixel ratio, and handle window resize (camera aspect + updateProjectionMatrix + renderer.setSize).
- Animation loop with requestAnimationFrame and a clock; move things by delta time, not per frame.
- Name the reusable parts (materials, geometries, factories). A scene you cannot repair is worse than a smaller scene that is clear.

Compose the frame
- Lighting: at least one ambient/hemisphere light plus one directional light. A scene with only MeshStandardMaterial and no light renders black.
- Use a key light with shadows, a cooler fill and, when useful, a warm rim. Set tone mapping and output color space deliberately; ACES filmic plus sRGB is a reliable default for a dark studio scene.
- Set a background or clear color on purpose. If the lower part of the frame is black, check the floor material, light direction and camera far plane.
- Frame the main subject centred at the default camera, filling roughly half to two-thirds of the frame width with a little headroom; nothing the task names is cut off or lost in a corner. Keep near/far tight enough for the scene size.
- Opaque trim, frames and seams must stay at the edges. Sanity-check their bounding dimensions: an opaque front/back mesh whose width and height are close to the whole enclosure face is a wall, not a seam, and will hide the subject.

Build recognizable objects, not primitives
- An object is a Group of several meshes with distinct silhouettes, proportions and materials. A single scaled sphere or box reads as a placeholder.
- Give the object a structure the viewer can name: a base, a body, a joint, an edge, a highlight. Proportions matter more than polygon count.
- For an organic or turned silhouette, a lathe profile is compact and controllable. Keep the profile in one place and reuse it for the parts that must follow the surface:
```js
// Radius as a function of height. Taper both ends so the object does not read as a pill.
const profile = [
  [0.02, -0.50], [0.14, -0.32], [0.20, -0.05],
  [0.16, 0.20], [0.06, 0.42], [0.01, 0.50],
].map(([r, y]) => new THREE.Vector2(r, y));
const body = new THREE.Mesh(new THREE.LatheGeometry(profile, 32), material);
```
- Use separate materials for parts that should read differently (matte body, brighter metal, emissive detail). Do not rely on one material for the whole object.
- Symmetry helps machinery; asymmetry helps organic forms. Whichever you choose, keep the parts aligned to a single local axis and orient moving objects along their velocity.

Materials and transparency
- The subject must be readable through every transparent surface. An enclosure that turns the interior pale, milky or frosted is a bug, not a premium look.
- Glass panels: `transparent: true` and `depthWrite: false`, but never `depthTest: false`. A panel drawn without the depth test composites over the subject and turns the whole scene into a frosted box. Do not stack several translucent sheets between the camera and the subject.
- A thin transparent surface plus a very faint tint reads better than a filled volume. Do not fill the volume with an emissive or opaque box, and do not raise fog density until the interior washes out.
- No full-screen CSS layer over the canvas: a `mix-blend-mode: screen` or blurred overlay, however subtle, is a veil on top of the scene. Keep light effects inside WebGL.
- Calibrate transparent details against the rendered frame, not against the parameter names. Transmission near 1 with very low opacity is the usual reason a small transparent detail disappears after tone mapping; raise opacity or a faint emissive before you raise its size.
- Small effects are judged at the scale they appear. A sparkle, a light shaft or a stream of small particles passes on presence and continuity at the camera distance, not on physical accuracy at full zoom. Do not make it opaque just to win a count.
- Many small scattered pieces (pebbles, chips, leaves, debris) read as a surface. Give them rough, non-metallic materials and varied but related colours; glossy pieces under a strong light turn into white speckle noise.
- Small drifting particles (droplets, sparks, dust) read as small translucent spheres or sprites with a bright rim or specular highlight and varied sizes. Large flat white discs read as opaque blobs; keep them small relative to the subject and let the highlight, not an opaque fill, carry visibility.

Motion and secondary motion
- Move by delta time; clamp large deltas after a tab pause.
- Secondary motion sells weight: a slight bob, a trailing part, a rotation phase. Vary speed, size and phase between copies.
- Many copies: share geometry and materials between instances, give each its own transform and phase, and prefer instancing when the count is large.

Environment
- Ground and a few props give the subject a place to stand. A subtle gradient or fog is enough for depth; do not hide the subject in it.
- A background the task specifies (a CSS gradient, a colour) must stay visible. A large opaque floor, room or backdrop mesh that fills the frame behind the subject replaces it; keep such props small, or render with `alpha: true` and no scene background so the page shows through.
- Fixtures are supported by something: a lamp hangs from cables or rests on a frame, a shelf spans its brackets, a machine sits on the floor. A part floating in the air or standing on legs that touch nothing reads as a mistake at first glance.
- Contents stay inside their container: a liquid, a filling or a pile fits the inner walls with a small margin, and a liquid surface sits just below the rim, never above it or poking through a wall.
- Objects sit in contact with the ground, not floating above it. Cast and receive shadows where the renderer supports it.
- Keep objects inside the scene bounds; steer or clamp them instead of letting them pop out.

Keep the implementation economical
- Prefer reusable constructors, shared materials/geometries and procedural loops over copied objects or huge hard-coded vertex/data tables. A polished self-contained scene should stay readable and compact enough to repair surgically.
- For a large single-file deliverable, do not spend one enormous model turn regenerating the whole file after it exists. Create a coherent first version, then use targeted edits. If one initial write would become unusually large, use bounded create/append chunks at stable section boundaries instead of one giant JSON tool argument.
- Keep module-level declaration names unique. A duplicate `const` or `class` at top level aborts the module before the first frame renders.

Before you finish
- No console errors. The page loads from a clean start and the first frame is not blank.
- If a visual checker is offered, call it and fix what it reports. The subject must be recognizable at first glance and the stated requirements must be visible in the frame.
- When the scene meets the requirements and the check passes, stop changing the look. Further decoration after a pass is how a clear scene becomes hazy; only fix a requirement the goal still names, then check again.