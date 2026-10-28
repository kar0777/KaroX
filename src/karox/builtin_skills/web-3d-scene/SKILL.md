---
name: web-3d-scene
version: 1.5.0
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

A clownfish skeleton that reads at the default camera. Adapt the materials and scale; keep the proportions and the eye size.
```js
// L = body length. Lathe profile: [radius, y], nose +0.5, tail -0.5.
const profile = [
  [0.010,-0.50],[0.085,-0.47],[0.150,-0.40],[0.195,-0.28],[0.210,-0.10],
  [0.195,0.08],[0.160,0.24],[0.110,0.38],[0.055,0.47],[0.010,0.50]
].map(([r, y]) => new THREE.Vector2(r, y));
const radiusAt = (t) => {           // t = nose fraction 0..1, for band radii
  for (let i = 1; i < profile.length; i += 1) {
    const t0 = 0.5 - profile[i-1].y, t1 = 0.5 - profile[i].y;
    if (t >= Math.min(t0,t1) && t <= Math.max(t0,t1)) {
      const f = (t - t0) / (t1 - t0 || 1);
      return profile[i-1].x + (profile[i].x - profile[i-1].x) * f;
    }
  }
  return 0.01;
};
function clownfish(L, bodyMat, whiteMat, blackMat, finMat) {
  const g = new THREE.Group();
  const bodyGeo = new THREE.LatheGeometry(profile, 24);
  bodyGeo.rotateZ(-Math.PI / 2);        // nose +X, tail -X
  const body = new THREE.Mesh(bodyGeo, bodyMat);
  body.scale.set(L, L, L * 0.55);       // taller than wide
  g.add(body);
  for (const t of [0.20, 0.47, 0.755]) { // three white bands with black rims
    const x = (0.5 - t) * L, r = radiusAt(t) * L;
    const band = new THREE.Mesh(new THREE.CylinderGeometry(r+0.004*L,r+0.004*L,0.055*L,24,1,true), whiteMat);
    band.rotation.z = Math.PI/2; band.scale.z = 0.55; band.position.x = x; g.add(band);
    for (const e of [-1, 1]) {
      const rim = new THREE.Mesh(new THREE.CylinderGeometry(r+0.006*L,r+0.006*L,0.010*L,24,1,true), blackMat);
      rim.rotation.z = Math.PI/2; rim.scale.z = 0.55; rim.position.x = x + e*0.032*L; g.add(rim);
    }
  }
  for (const s of [-1, 1]) {            // big protruding eyes: white, pupil
    const eye = new THREE.Mesh(new THREE.SphereGeometry(0.050*L, 12, 10), whiteMat);
    eye.position.set(0.32*L, 0.08*L, s*0.090*L); g.add(eye);
    const pupil = new THREE.Mesh(new THREE.SphereGeometry(0.028*L, 10, 8), blackMat);
    pupil.position.set(0.332*L, 0.08*L, s*0.120*L); g.add(pupil);
  }
  const mouth = new THREE.Mesh(new THREE.SphereGeometry(0.030*L, 10, 8), blackMat);
  mouth.position.set(0.485*L, -0.035*L, 0); g.add(mouth);
  const tailShape = new THREE.Shape();
  tailShape.moveTo(0,0); tailShape.lineTo(-0.26,0.17); tailShape.lineTo(-0.30,0);
  tailShape.lineTo(-0.26,-0.17); tailShape.closePath();
  const tailPivot = new THREE.Group(); tailPivot.position.x = -0.47*L; g.add(tailPivot);
  const tail = new THREE.Mesh(new THREE.ExtrudeGeometry(tailShape,{depth:0.02*L,bevelEnabled:false}), finMat);
  tail.scale.set(L, L, 1); tailPivot.add(tail);
  const dorsalShape = new THREE.Shape();
  dorsalShape.moveTo(-0.18,0); dorsalShape.quadraticCurveTo(0.02,0.20,0.22,0); dorsalShape.closePath();
  const dorsal = new THREE.Mesh(new THREE.ExtrudeGeometry(dorsalShape,{depth:0.015*L,bevelEnabled:false}), finMat);
  dorsal.position.set(0.05*L, 0.17*L, -0.0075*L); dorsal.scale.set(L, L, 1); g.add(dorsal);
  return { group: g, tailPivot };
}
// tailPivot.rotation.y = Math.sin(t * 6 + phase) * 0.45;  // continuous wag
```
Paste this skeleton once. If the file already has a fish factory, adapt that one instead of adding a second; if the module already uses `profile` or `radiusAt`, give the two helpers a unique prefix. A duplicate declaration breaks the page before any check can judge it.

If the check still calls the fish a lozenge or says the eyes are invisible, the body profile or the eye radius is the cause, not the band colors. Do not fix that by making the fish larger.

Bubbles that survive tone mapping. Two emitters at the floor, a shared pool of small spheres, 5-6 spawns per second per emitter, and eight bubbles already in each column on frame one.
```js
const bubbleMat = new THREE.MeshPhysicalMaterial({
  color: 0xffffff, roughness: 0.03, metalness: 0.0,
  transmission: 0.25, thickness: 0.02, ior: 1.25,
  transparent: true, opacity: 0.60, depthWrite: false,
  clearcoat: 1.0, clearcoatRoughness: 0.02,
  emissive: 0x9fd8ff, emissiveIntensity: 0.22, envMapIntensity: 2.0
});
// radius 0.05-0.11 world units, speed 0.45-0.9, slight sine wobble;
// prefill each emitter with 8 bubbles spread over its column.
```
Transmission near 1 with opacity 0.16 makes bubbles invisible after the tone mapper; that is the usual cause of "no bubbles" in the check. If they still disappear, raise opacity before raising the radius.

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
- Build the hood on the tank rim with two short brackets, not on tall posts floating above the tank. Put a warm emissive bar inside it (emissive `#ffe0b2`) and a real `PointLight(0xffe0b2, ~15-25, 12, 2)` near the water; a tan slab with no light is not a lamp. Tune the intensity until the sand under it is visibly warmer than the corners.
- Exactly N fish means N fish visible at once from the starting camera. Spread their start positions in x, z and height, and do not give two of them the same path; if the check counts fewer, change positions before changing sizes.
- Bubbles must survive the tone mapping: at the default camera each bubble is at least ~0.08 world units across, mostly white with a bright rim, and one stream shows 8–12 bubbles between the floor and the surface at once. Spawn 4–6 per second per emitter so the column is continuous, not a few isolated specks.
- The waterline, ripples and gravel texture are checked in the same frame. If the check says one is missing, fix that effect and re-check; do not rebuild the whole scene.

Before you finish
- No console errors. Page loads from a clean start.
- If visual_check is offered, call it and fix what it reports. The subject must be recognizable at first glance: fish must look like fish, not balls or capsules.
- When visual_check passes, stop changing the look. Further decoration after a pass is how a clear scene becomes hazy; only fix a requirement the goal still names, then check again.
