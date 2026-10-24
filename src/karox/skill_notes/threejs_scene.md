# three.js scene notes

Checklist for a page that must render a lit 3D scene. Each line is a defect that
has shipped in a measured run, so treat them as acceptance criteria rather than
suggestions.

1. Renderer: set `renderer.outputColorSpace = THREE.SRGBColorSpace`, use
   `renderer.toneMapping = THREE.ACESFilmicToneMapping` with
   `toneMappingExposure` between 1.0 and 1.4. Without tone mapping a lit scene
   either washes out to white or crushes to black.
2. Light with at least three sources: a `HemisphereLight` for ambient fill, a
   `DirectionalLight` for the main direction, and a weak `AmbientLight`. The
   bottom of the frame must never be black.
3. Materials: `MeshStandardMaterial` or `MeshPhysicalMaterial` with `roughness`
   between 0.3 and 0.8. Never use pure black as a `color`; a black material
   reflects no light and reads as a hole in the scene.
4. Thin parts (fins, leaves, cloth, wings) need volume: either a real thickness
   greater than zero or `side: THREE.DoubleSide`, otherwise they disappear when
   seen edge-on.
5. Creature and object silhouettes: build them from several primitives scaled
   per axis and positioned as a group. A single sphere or box reads as a
   placeholder, not as the thing that was asked for.
6. Imports: load `three` only from the version the task allows, through an
   import map, and load addons from the same CDN at the same version. A missing
   bare mapping for `three` breaks every addon that imports it.
7. Animate with `renderer.setAnimationLoop` and take time from `THREE.Clock`;
   drive motion from elapsed time, never from frame counts or `Date.now()`.
8. Expose counters for automated checks: `window.__karoProbe = { objects: {
   name: count, ... } }`, filled after the scene is built. The page check reads
   this to tell an empty scene from a full one.
