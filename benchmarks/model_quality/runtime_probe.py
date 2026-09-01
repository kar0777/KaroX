"""Playwright runtime acceptance probe for generated model-quality HTML pages."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


def _check(check_id: str, description: str, passed: bool, detail: Any = None, weight: int = 1) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "description": description,
        "passed": bool(passed),
        "weight": int(weight),
        "detail": detail,
    }


def _frontend(page: Any) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    ready = page.evaluate("() => window.__benchmarkReady === true")
    checks.append(_check("runtime_ready", "benchmark ready flag", ready, weight=2))
    desktop = page.evaluate(
        """() => ({
          width: innerWidth,
          scrollWidth: document.documentElement.scrollWidth,
          metrics: document.querySelectorAll('[data-metric-card]').length,
          rows: document.querySelectorAll('[data-activity-table] tbody tr').length,
          h1: document.querySelector('h1')?.textContent?.trim() || ''
        })"""
    )
    checks.append(_check("desktop_overflow", "no desktop horizontal overflow", desktop["scrollWidth"] <= desktop["width"] + 1, desktop, 2))
    checks.append(_check("runtime_metrics", "four runtime metric cards", desktop["metrics"] == 4, desktop["metrics"], 2))
    checks.append(_check("runtime_rows", "at least five activity rows", desktop["rows"] >= 5, desktop["rows"]))
    checks.append(_check("runtime_h1", "visible h1", bool(desktop["h1"]), desktop["h1"]))

    theme = page.evaluate(
        """() => {
          const buttons=[...document.querySelectorAll('button')];
          const b=buttons.find(x => /theme|light|dark/i.test((x.textContent||'')+' '+(x.getAttribute('aria-label')||'')+' '+(x.title||'')));
          if(!b) return {found:false,changed:false};
          const before=document.documentElement.dataset.theme || '';
          b.click();
          const after=document.documentElement.dataset.theme || '';
          return {found:true,before,after,changed:before!==after && !!after};
        }"""
    )
    checks.append(_check("runtime_theme", "theme button changes dataset.theme", theme.get("found") and theme.get("changed"), theme, 2))

    menu = page.evaluate(
        """() => {
          const b=[...document.querySelectorAll('button[aria-expanded]')][0];
          if(!b) return {found:false,changed:false};
          const before=b.getAttribute('aria-expanded'); b.click();
          const after=b.getAttribute('aria-expanded');
          return {found:true,before,after,changed:before!==after};
        }"""
    )
    checks.append(_check("runtime_menu", "mobile-menu state toggles", menu.get("found") and menu.get("changed"), menu, 2))

    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(150)
    mobile = page.evaluate("() => ({width:innerWidth,scrollWidth:document.documentElement.scrollWidth})")
    checks.append(_check("mobile_overflow", "no mobile horizontal overflow", mobile["scrollWidth"] <= mobile["width"] + 1, mobile, 3))
    return checks


def _scene_snapshot(page: Any) -> dict[str, Any]:
    return page.evaluate(
        """() => {
          const s=window.__aquariumScene;
          if(!s || typeof s.traverse!=='function') return {available:false};
          const names=[]; const fish=[]; let meshCount=0;
          s.traverse(o=>{
            if(o.name) names.push(o.name);
            if(o.isMesh) meshCount++;
            if(/^clownfish-[123]$/.test(o.name||'')) fish.push({name:o.name,x:o.position.x,y:o.position.y,z:o.position.z,q:[o.quaternion.x,o.quaternion.y,o.quaternion.z,o.quaternion.w]});
          });
          const water=s.getObjectByName('water-surface');
          let waterSample=null;
          if(water){
            const a=water.geometry?.attributes?.position?.array;
            waterSample={p:[water.position.x,water.position.y,water.position.z],r:[water.rotation.x,water.rotation.y,water.rotation.z],v:a?Array.from(a.slice(0,18)):[]};
          }
          return {available:true,names,fish,meshCount,waterSample};
        }"""
    )


def _changed(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True)


def _three_d(page: Any) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    ready = page.evaluate("() => window.__benchmarkReady === true")
    checks.append(_check("runtime_ready", "benchmark ready flag", ready, weight=2))
    first = _scene_snapshot(page)
    checks.append(_check("scene_available", "actual THREE.Scene exposed", first.get("available"), weight=3))
    if not first.get("available"):
        return checks
    names = first["names"]
    glass = [n for n in names if n in {"glass-front","glass-back","glass-left","glass-right","glass-bottom"}]
    checks.append(_check("runtime_glass", "exactly five required glass panels", len(glass) == 5 and "glass-top" not in names, glass, 3))
    checks.append(_check("runtime_water_volume", "water volume exists", names.count("water-volume") == 1, names.count("water-volume"), 2))
    checks.append(_check("runtime_water_surface", "water surface exists", names.count("water-surface") == 1, names.count("water-surface"), 2))
    fish_names = sorted(item["name"] for item in first["fish"])
    checks.append(_check("runtime_fish", "exactly three clownfish roots", fish_names == ["clownfish-1","clownfish-2","clownfish-3"], fish_names, 4))
    checks.append(_check("scene_richness", "substantial procedural mesh scene", first["meshCount"] >= 20, first["meshCount"]))

    page.wait_for_timeout(850)
    second = _scene_snapshot(page)
    fish_moved = first.get("fish") and _changed(first.get("fish"), second.get("fish"))
    water_moved = first.get("waterSample") is not None and _changed(first.get("waterSample"), second.get("waterSample"))
    checks.append(_check("runtime_fish_motion", "fish positions/orientations animate", bool(fish_moved), weight=3))
    checks.append(_check("runtime_water_motion", "water surface transform/geometry animates", bool(water_moved), weight=3))

    before = page.evaluate("() => ({w:innerWidth,h:innerHeight,aspect:window.__aquariumScene?.userData?.camera?.aspect || null})")
    page.set_viewport_size({"width": 640, "height": 760})
    page.wait_for_timeout(200)
    canvas = page.evaluate("() => {const c=document.querySelector('canvas'); return c?{w:c.clientWidth,h:c.clientHeight}:null}")
    checks.append(_check("runtime_resize", "renderer canvas follows viewport resize", bool(canvas and abs(canvas["w"]-640)<=2 and abs(canvas["h"]-760)<=2), {"before":before,"after":canvas}, 2))
    return checks


def run(kind: str, path: Path) -> dict[str, Any]:
    target = path.expanduser().resolve(strict=True)
    if not target.is_file():
        raise ValueError("benchmark target must be a file")
    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []
    started = time.perf_counter()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width":1280,"height":800}, device_scale_factor=1)
        page.on("console", lambda msg: console_errors.append(msg.text[:500]) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: page_errors.append(str(exc)[:500]))
        page.on("requestfailed", lambda req: failed_requests.append(req.url[:500]))
        try:
            page.goto(target.as_uri(), wait_until="load", timeout=30_000)
            try:
                page.wait_for_function("() => window.__benchmarkReady === true", timeout=12_000)
            except PlaywrightError:
                # Keep collecting evidence; ready is itself a scored check.
                page.wait_for_timeout(600)
            checks = _frontend(page) if kind == "frontend" else _three_d(page)
            screenshot = str(target.with_suffix(f".{kind}.png"))
            page.screenshot(path=screenshot, full_page=True)
        finally:
            browser.close()
    # Source maps are not required benchmark assets.
    meaningful_failed = [url for url in failed_requests if not url.endswith(".map")]
    checks.append(_check("no_console_errors", "no console errors", not console_errors, console_errors, 3))
    checks.append(_check("no_page_errors", "no page errors", not page_errors, page_errors, 3))
    checks.append(_check("no_failed_requests", "no failed required requests", not meaningful_failed, meaningful_failed, 2))
    passed = sum(item["weight"] for item in checks if item["passed"])
    total = sum(item["weight"] for item in checks)
    return {
        "kind": kind,
        "path": str(target),
        "score": round(passed / total, 4) if total else 0.0,
        "passed_weight": passed,
        "total_weight": total,
        "checks": checks,
        "console_errors": console_errors,
        "page_errors": page_errors,
        "failed_requests": meaningful_failed,
        "screenshot": screenshot,
        "duration_ms": round((time.perf_counter()-started)*1000, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("frontend","3d"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.kind, args.path), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
