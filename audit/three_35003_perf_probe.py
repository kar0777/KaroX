"""Independent offline Chromium frame-time probe for a real Three.js workspace.

Diagnostic only: never a scored issue grader or CLI benchmark result.
Measures actual requestAnimationFrame cadence with a fixed viewport, real
WebGL canvas and software SwiftShader. Not a hardware-GPU FPS claim.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import io
import json
import statistics
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageStat
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def percentile(samples: list[float], percentage: float) -> float:
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentage)))
    return round(ordered[index], 3)


def measure(workspace: Path, output: Path, samples: int = 120) -> dict:
    workspace = workspace.resolve()
    output = output.resolve()
    if not workspace.is_dir() or not workspace.is_relative_to(ROOT):
        raise ValueError("Only explicitly staged lab workspaces may be measured")
    if output.exists():
        raise FileExistsError("Refusing to overwrite a prior browser proof")
    if not 30 <= samples <= 240:
        raise ValueError("Frame samples must be 30..240")
    output.mkdir(parents=True)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(SimpleHTTPRequestHandler, directory=str(workspace))
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--ignore-gpu-blocklist", "--use-angle=swiftshader",
                    "--enable-unsafe-swiftshader", "--enable-webgl",
                ],
            )
            try:
                page = browser.new_page(viewport={"width": 1200, "height": 900})
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(
                    f"http://127.0.0.1:{server.server_address[1]}/index.html",
                    wait_until="load", timeout=25000,
                )
                canvas = page.locator("#scene")
                canvas.wait_for(state="visible")
                page.wait_for_timeout(700)
                gl = canvas.evaluate(
                    "c => { const gl=c.getContext('webgl2') || c.getContext('webgl'); "
                    "return gl ? { version: gl.getParameter(gl.VERSION), "
                    "lost: gl.isContextLost() } : null; }"
                )
                assert gl and not gl["lost"], "No live WebGL context"
                frame_ms = page.evaluate(
                    """async (samples) => {
                        const intervals = [];
                        await new Promise(resolve => {
                            let previous = null;
                            function collect(timestamp) {
                                if (previous !== null) intervals.push(timestamp - previous);
                                previous = timestamp;
                                if (intervals.length < samples)
                                    requestAnimationFrame(collect);
                                else resolve();
                            }
                            requestAnimationFrame(collect);
                        });
                        return intervals;
                    }""",
                    samples,
                )
                frame_ms = [float(x) for x in frame_ms if 0 < float(x) < 3000]
                assert len(frame_ms) == samples, "Browser did not draw expected rAF count"
                png = canvas.screenshot()
                (output / "canvas.png").write_bytes(png)
                image = Image.open(io.BytesIO(png)).convert("RGB")
                spread = max(ImageStat.Stat(image).stddev)
                assert spread > 10, "3D canvas is blank"
                assert not errors, "Browser crashed: " + repr(errors)
                result = {
                    "status": "PASS",
                    "workspace": str(workspace),
                    "scoreboard": False,
                    "fixed_scene_and_viewport": [1200, 900],
                    "browser_version": browser.version,
                    "render_backend": "Chromium SwiftShader software WebGL",
                    "webgl_version": gl["version"],
                    "samples": len(frame_ms),
                    "frame_ms_median": round(statistics.median(frame_ms), 3),
                    "frame_ms_p95": percentile(frame_ms, 0.95),
                    "frame_ms_mean": round(statistics.mean(frame_ms), 3),
                    "effective_fps_from_mean_rAF_ms": round(1000 / statistics.mean(frame_ms), 2),
                    "pixel_stddev_max": round(spread, 3),
                    "canvas_png_sha256": hashlib.sha256(png).hexdigest(),
                    "browser_page_errors": errors,
                    "limitations": (
                        "rAF cadence at this host/viewport, not GPU time, physical "
                        "monitor refresh or user-visible frame rate on real hardware"
                    ),
                }
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
    (output / "perf_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=120)
    args = parser.parse_args()
    result = measure(args.workspace, args.out, args.samples)
    print(json.dumps(result, ensure_ascii=False, indent=2))
