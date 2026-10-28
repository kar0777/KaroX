---
name: web-3d-scene
version: 1.2.0
description: Build or fix a three.js scene that runs in the browser.
when_to_use: 3D, three.js, WebGL, animated scenes, aquariums, particles, models in a page.
triggers: [three, webgl, canvas, "*.html"]
---
# three.js scene recipe

Setup
- Use the three.js version the task or repo pins. Load it the way the repo already does (import map, local file, bundler). Before using a class, confirm it exists in that version; newer helpers may be missing.
- One renderer, one scene, one camera. Set renderer size and pixel ratio, and handle window resize (camera aspect + updateProjectionMatrix + renderer.setSize).
- Animation loop with requestAnimationFrame and a clock; move things by delta time, not per frame.

Make it readable on screen
- Lighting: at least one ambient/hemisphere light plus one directional light. A scene with only MeshStandardMaterial and no light renders black.
- Set a background or clear color on purpose. If the lower part of the frame is black, check the floor material, light direction and camera far plane.
- Put the camera so the main subject fills the middle third of the frame. Keep near/far tight enough for the scene size.
- Opaque trim, frames and seams must stay at the edges. Sanity-check their bounding dimensions: an opaque front/back mesh whose width *and* height are close to the whole enclosure face is a wall, not a seam, and will hide the scene. Glass interiors must remain visibly readable through the front panel.

Build recognizable objects, not primitives
- A creature or object is a Group of several meshes. A fish: elongated body (scaled sphere or LatheGeometry), a tail (flattened cone or shape) at the back, two side fins, a top fin, eyes. Color the parts differently.
- Proportions: the body tapers toward the tail and has a distinct head; no uniform capsule or cylinder. Fins are smaller than the body (tail about a quarter of body length) and use DoubleSide so they do not vanish edge-on. Eyes are clearly visible.
- Real species keep their real colors: a clownfish has an orange body, white bands with thin black edges, and orange fins with black rims. Check the colors against the named species before finishing.
- Orient each moving object along its velocity (lookAt position + velocity) so it swims forward, not sideways.
- Secondary motion sells it: tail and fins swing with a sine of time; the body bobs slightly. Vary speed, size and color per instance.
- Many copies: share geometry and materials between instances; give each its own position, velocity and phase.

Keep the implementation economical
- Prefer reusable constructors, shared materials/geometries and procedural loops over copied objects or huge hard-coded vertex/data tables. A polished self-contained scene should stay readable and compact enough to repair surgically.
- For a large single-file deliverable, do not spend one enormous model turn regenerating the whole file after it exists. Create a coherent first version, then use targeted edits. If one initial write would become unusually large, use bounded create/append chunks at stable section boundaries instead of one giant JSON tool argument.

Clear materials, not haze
- The subject must be readable through every transparent surface. A tank, window or glass panel that turns the interior pale, milky or frosted is a bug, not a premium look. The gravel, the far wall and every fish must stay clearly visible.
- Glass panels: `transparent: true` and `depthWrite: false`, but never `depthTest: false`. A panel drawn without the depth test composites over the subject and turns the whole scene into a frosted box. Do not stack several translucent sheets between the camera and the subject.
- Water: a thin, mostly transparent surface plus a very faint tint. Do not fill the volume with an emissive or opaque box, and do not raise fog density until the interior washes out. If the far wall looks like it is behind fog, reduce the tint until it is invisible at the far wall.
- No full-screen CSS layer over the canvas: a `mix-blend-mode: screen` or blurred overlay, however subtle, is a veil on top of the scene. Keep light effects inside WebGL.
- Bubble/sparkle particles are small and mostly transparent; opaque white spheres read as ping-pong balls.

Environment
- Ground (sand plane with a lighter color) and a few props (rocks, plants as groups of thin boxes or cones that sway). A slight blue gradient in the background is enough for depth.
- Rocks sit partly sunk into the sand, flattened and irregular, in muted grey-brown; not white eggs lying on top. Plants: several bent blades per plant, and several plants.
- Light patterns on the sand must be soft and irregular, never a visible repeating grid.
- Keep objects inside bounds: steer away from walls smoothly instead of teleporting.

Effects a frame has to show
- A light exists in the code only if its effect exists in the frame. A warm hood lamp must leave a visibly warmer pool on the sand and the top glass; if the tank still reads uniform cool blue, raise the lamp and lower the cool fill. Check the rendered frame, not the light's parameters.
- Bubbles must survive the tone mapping: at the default camera each bubble is at least ~0.08 world units across, mostly white with a bright rim, and one stream shows 8–12 bubbles between the floor and the surface at once. Spawn 4–6 per second per emitter so the column is continuous, not a few isolated specks.
- The waterline, ripples and gravel texture are checked in the same frame. If the check says one is missing, fix that effect and re-check; do not rebuild the whole scene.

Before you finish
- No console errors. Page loads from a clean start.
- If visual_check is offered, call it and fix what it reports. The subject must be recognizable at first glance: fish must look like fish, not balls or capsules.
- When visual_check passes, stop changing the look. Further decoration after a pass is how a clear scene becomes hazy; only fix a requirement the goal still names, then check again.
