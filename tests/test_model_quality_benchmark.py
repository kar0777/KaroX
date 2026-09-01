from __future__ import annotations

from karox.model_quality_benchmark import (
    FRONTEND_TASK,
    THREE_D_TASK,
    evaluate_source,
    failed_check_ids,
    karo_system_prompt,
    repair_prompt,
)


FRONTEND_GOOD = r"""<!doctype html><html data-theme="dark"><head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>:root{--a:1;--b:2;--c:3;--d:4;--e:5;--f:6}.x:focus-visible{outline:2px solid}
@media(max-width:768px){nav{display:none}}@media(prefers-reduced-motion:reduce){*{animation:none}}</style></head>
<body><header>Northstar<nav>Nav</nav><button id="menu" aria-expanded="false">Menu</button><button id="theme">Theme</button></header>
<main><section><h1>Overview</h1><svg><path d="M0 0L1 1"/></svg></section><section>
<div data-metric-card>1</div><div data-metric-card>2</div><div data-metric-card>3</div><div data-metric-card>4</div>
</section><section><table data-activity-table><thead><tr><th>A</th></tr></thead><tbody>
<tr><td>1</td></tr><tr><td>2</td></tr><tr><td>3</td></tr><tr><td>4</td></tr><tr><td>5</td></tr>
</tbody></table></section></main><footer>Done</footer><script>
menu.addEventListener('click',()=>menu.setAttribute('aria-expanded', menu.getAttribute('aria-expanded')==='false'?'true':'false'));
theme.addEventListener('click',()=>document.documentElement.dataset.theme=document.documentElement.dataset.theme==='dark'?'light':'dark');
window.__benchmarkReady=true;</script></body></html>"""


THREE_GOOD = r"""<!doctype html><html><head><script type="importmap">{"imports":{"three":"https://unpkg.com/three@0.160.0/build/three.module.js","three/addons/":"https://unpkg.com/three@0.160.0/examples/jsm/"}}</script></head><body>
<script type="module">import * as THREE from 'three'; import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
const scene=new THREE.Scene(); const renderer=new THREE.WebGLRenderer(); const camera=new THREE.PerspectiveCamera();
const controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=true;
['glass-front','glass-back','glass-left','glass-right','glass-bottom'].forEach(n=>{const m=new THREE.Mesh(new THREE.BoxGeometry(),new THREE.MeshPhysicalMaterial());m.name=n;scene.add(m)});
const water=new THREE.Mesh(new THREE.BoxGeometry(),new THREE.MeshPhysicalMaterial());water.name='water-volume';scene.add(water);
const surface=new THREE.Mesh(new THREE.PlaneGeometry(),new THREE.MeshPhysicalMaterial());surface.name='water-surface';scene.add(surface);
function fish(n){const g=new THREE.Group();g.name=n; const body=new THREE.Mesh(new THREE.SphereGeometry(),new THREE.MeshStandardMaterial({color:'orange'}));g.add(body); const whiteBand=new THREE.Mesh(new THREE.BoxGeometry(),new THREE.MeshStandardMaterial({color:'white'}));g.add(whiteBand); const tail=new THREE.Mesh(new THREE.ConeGeometry(),new THREE.MeshStandardMaterial());g.add(tail); const eye=new THREE.Mesh(new THREE.SphereGeometry(),new THREE.MeshStandardMaterial());g.add(eye); const fin=new THREE.Mesh(new THREE.ConeGeometry(),new THREE.MeshStandardMaterial());g.add(fin);scene.add(g);return g}
const fish1=fish('clownfish-1'),fish2=fish('clownfish-2'),fish3=fish('clownfish-3');
const rock=new THREE.Mesh(new THREE.DodecahedronGeometry(),new THREE.MeshStandardMaterial()); const plant=new THREE.Group(); const bubble=new THREE.Mesh(new THREE.SphereGeometry(),new THREE.MeshBasicMaterial());scene.add(rock,plant,bubble);
scene.add(new THREE.HemisphereLight(),new THREE.DirectionalLight());
addEventListener('resize',()=>renderer.setSize(innerWidth,innerHeight));function animate(t){requestAnimationFrame(animate);surface.position.y=Math.sin(t*.001);fish1.position.x=Math.cos(t*.001);fish1.rotation.y=Math.sin(t*.001);controls.update();renderer.render(scene,camera)}
window.__aquariumScene=scene;window.__benchmarkReady=true;requestAnimationFrame(animate);</script></body></html>"""


def test_frontend_reference_hits_every_static_check() -> None:
    result = evaluate_source("frontend", FRONTEND_GOOD)
    assert result.score == 1.0, failed_check_ids(result)


def test_three_reference_hits_every_static_check() -> None:
    result = evaluate_source("3d", THREE_GOOD)
    assert result.score == 1.0, failed_check_ids(result)


def test_incomplete_page_fails_multiple_requirements() -> None:
    result = evaluate_source("frontend", "<!doctype html><h1>Northstar</h1>")
    assert result.score < 0.4
    assert "viewport" in failed_check_ids(result)
    assert "metric_cards" in failed_check_ids(result)


def test_task_contracts_are_frozen_and_specific() -> None:
    assert "Exactly four metric cards" in FRONTEND_TASK
    assert "exactly five glass panels" in THREE_D_TASK
    assert "Exactly three procedural clownfish" in THREE_D_TASK


def test_karo_prompt_and_repair_do_not_hide_acceptance_failures() -> None:
    system = karo_system_prompt("frontend")
    assert "acceptance" in system.lower()
    repair = repair_prompt("frontend", "<html></html>", ["metric_cards", "theme_toggle"])
    assert "metric_cards" in repair
    assert "theme_toggle" in repair
    assert "CURRENT HTML" in repair
