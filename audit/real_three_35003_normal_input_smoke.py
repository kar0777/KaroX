"""Independent non-scored normal mouse / pen / touch regression checks.

The mixed release issue is scored by real_three_35003_grader_v2.py. These
separate probes check that a proposed reference fix does not break ordinary
input; they do not alter the frozen scorer or become extra scored tests.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from PIL import Image, ImageStat
from playwright.sync_api import sync_playwright


def number(page, selector: str) -> float:
    return float(page.locator(selector).inner_text().split(":")[-1].strip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--proof", type=Path, required=True)
    args = parser.parse_args()
    ws = args.workspace.resolve()
    proof = args.proof.resolve()
    if proof.exists():
        parser.error(f"Evidence already exists: {proof}")
    if not (ws / "OrbitControls.js").is_file():
        parser.error("Missing real OrbitControls.js")
    proof.mkdir(parents=True)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(ws))
    )
    Thread(target=server.serve_forever, daemon=True).start()
    outcomes = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--ignore-gpu-blocklist",
                    "--use-angle=swiftshader",
                    "--enable-unsafe-swiftshader",
                    "--enable-webgl",
                ],
            )
            try:
                for mode in ("mouse", "pen", "touch"):
                    result = {"mode": mode, "pass": False}
                    context = browser.new_context(
                        viewport={"width": 1200, "height": 900}, has_touch=mode == "touch"
                    )
                    page = context.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    try:
                        page.goto(
                            f"http://127.0.0.1:{server.server_address[1]}/index.html",
                            wait_until="load",
                            timeout=20000,
                        )
                        canvas = page.locator("#scene")
                        canvas.wait_for(state="visible", timeout=10000)
                        assert canvas.evaluate(
                            "c => !!(c.getContext('webgl2') || c.getContext('webgl'))"
                        ), "No real WebGL context"
                        page.wait_for_timeout(500)
                        shot = canvas.screenshot(timeout=12000)
                        img = Image.open(io.BytesIO(shot)).convert("RGB")
                        assert max(ImageStat.Stat(img).stddev) > 10, "Blank 3D scene"
                        (proof / (mode + ".png")).write_bytes(shot)
                        box = canvas.bounding_box()
                        assert box
                        x = box["x"] + box["width"] / 2
                        y = box["y"] + box["height"] / 2
                        cdp = context.new_cdp_session(page)
                        start_ends = number(page, "#ends")
                        start_angle = number(page, "#azimuth")
                        if mode == "touch":
                            def touch(event_type, coords):
                                cdp.send(
                                    "Input.dispatchTouchEvent",
                                    {"type": event_type, "touchPoints": coords},
                                )
                            touch("touchStart", [{"x": x, "y": y, "id": 7}])
                            for i in range(1, 7):
                                touch("touchMove", [{"x": x + 16 * i, "y": y, "id": 7}])
                            touch("touchEnd", [])
                        else:
                            def pointer(kind, cx, cy, button="none", buttons=0, clicks=0):
                                cdp.send(
                                    "Input.dispatchMouseEvent",
                                    {
                                        "type": kind,
                                        "x": cx,
                                        "y": cy,
                                        "button": button,
                                        "buttons": buttons,
                                        "clickCount": clicks,
                                        "pointerType": mode,
                                    },
                                )
                            pointer("mousePressed", x, y, "left", 1, 1)
                            for i in range(1, 7):
                                pointer("mouseMoved", x + 16 * i, y, "left", 1)
                            pointer("mouseReleased", x + 96, y, "left", 0, 1)
                        page.wait_for_timeout(120)
                        finish_ends = number(page, "#ends")
                        finish_angle = number(page, "#azimuth")
                        assert not errors, f"Page exceptions: {errors!r}"
                        assert finish_ends == start_ends + 1, "Gesture did not end exactly once"
                        assert abs(finish_angle - start_angle) > 0.02, "3D camera did not rotate"
                        result.update(
                            {
                                "pass": True,
                                "start_ends": start_ends,
                                "finish_ends": finish_ends,
                                "azimuth_change": round(finish_angle - start_angle, 5),
                            }
                        )
                    except Exception as error:
                        result["error"] = f"{type(error).__name__}: {error}"
                    finally:
                        result["page_errors"] = errors
                        outcomes.append(result)
                        context.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
    images = []
    for path in sorted(proof.glob("*.png")):
        images.append(
            {
                "filename": path.name,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    report = {"workspace": str(ws), "scored": False, "outcomes": outcomes, "images": images}
    (proof / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(result["pass"] for result in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
