---
name: web-3d-scene
version: 1.0.0
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

Environment
- Ground (sand plane with a lighter color), a few props (rocks, plants as groups of thin boxes or cones that sway), and light fog or a blue gradient for water depth.
- Rocks sit partly sunk into the sand, flattened and irregular, in muted grey-brown; not white eggs lying on top. Plants: several bent blades per plant, and several plants.
- Light patterns on the sand must be soft and irregular, never a visible repeating grid. The water volume should read as water: a slight blue tint and depth fog inside the tank.
- Keep objects inside bounds: steer away from walls smoothly instead of teleporting.

Before you finish
- No console errors. Page loads from a clean start.
- If visual_check is offered, call it and fix what it reports. The subject must be recognizable at first glance: fish must look like fish, not balls or capsules.
