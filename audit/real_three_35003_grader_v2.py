# DEV v2: real WebGL2 created by Three.js must not be rejected as missing WebGL.
# Original grader preserved unchanged; initial false RED classified GRADER_INVALID.
"""Actual Chrome CDP pointer regression from public Three.js issue #35003.

A real 3D WebGL page is rendered locally, no CDN. A mismatch pen-down then
mouse-up must dispatch end, avoid TypeError and stop rotating on hover.
"""
from __future__ import annotations
import functools
import os
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import threading
from playwright.sync_api import sync_playwright

ROOT=Path(os.environ["TASK_WORKSPACE"]).resolve()
PROOFS=Path(os.environ["VISUAL_PROOF_DIR"]).resolve()
PROOFS.mkdir(parents=True,exist_ok=True)

def test_real_browser_mixed_pen_mouse_release():
    handler=functools.partial(SimpleHTTPRequestHandler,directory=str(ROOT))
    server=ThreadingHTTPServer(("127.0.0.1",0),handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True,args=[
                "--ignore-gpu-blocklist","--use-angle=swiftshader",
                "--enable-unsafe-swiftshader","--enable-webgl"])
            try:
                page=browser.new_page(viewport={"width":1200,"height":900})
                errors=[]
                page.on("pageerror",lambda err: errors.append(str(err)))
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/index.html",
                          wait_until="load",timeout=25000)
                canvas=page.locator("#scene")
                canvas.wait_for(state="visible")
                assert canvas.evaluate("c => !!(c.getContext('webgl2') || c.getContext('webgl'))"),"WebGL missing"
                page.wait_for_timeout(900)
                snapshot=canvas.screenshot()
                (PROOFS/"scene_before.png").write_bytes(snapshot)
                from PIL import Image,ImageStat
                import io
                im=Image.open(io.BytesIO(snapshot)).convert("RGB")
                assert max(ImageStat.Stat(im).stddev)>10,"WebGL 3D scene not visible"
                rect=canvas.bounding_box()
                assert rect and rect["width"]>500 and rect["height"]>400
                x=rect["x"]+rect["width"]/2
                y=rect["y"]+rect["height"]/2
                cdp=page.context.new_cdp_session(page)
                def send(event_type,x,y,*,button="none",buttons=0,pointer="mouse",clicks=0):
                    return cdp.send("Input.dispatchMouseEvent",{
                       "type":event_type,"x":x,"y":y,"button":button,"buttons":buttons,
                       "clickCount":clicks,"pointerType":pointer})
                angle_initial=float(page.locator("#azimuth").inner_text().split(":")[-1])
                send("mousePressed",x,y,button="left",buttons=1,pointer="pen",clicks=1)
                for i in range(1,7):
                    send("mouseMoved",x+i*15,y,button="left",buttons=1,pointer="pen")
                angle_drag=float(page.locator("#azimuth").inner_text().split(":")[-1])
                assert abs(angle_drag-angle_initial)>0.03,"The 3D camera must really orbit during the pressed pen drag"
                send("mouseReleased",x+90,y,button="left",buttons=0,pointer="mouse",clicks=1)
                page.wait_for_timeout(150)
                before=int(page.locator("#ends").inner_text().split(":")[-1])
                angle_before=float(page.locator("#azimuth").inner_text().split(":")[-1])
                for i in range(1,11):
                    send("mouseMoved",x+90-i*25,y+i*8,buttons=0,pointer="mouse")
                page.wait_for_timeout(100)
                after=int(page.locator("#ends").inner_text().split(":")[-1])
                angle_after=float(page.locator("#azimuth").inner_text().split(":")[-1])
                (PROOFS/"event_results.json").write_text(
                    __import__("json").dumps({
                        "page_errors":errors,"end_before_hover":before,
                        "end_after_hover":after,
                        "azimuth_before_hover":angle_before,
                        "azimuth_after_hover":angle_after
                    },indent=2),encoding="utf-8")
                assert not errors,"Browser runtime crashed: "+repr(errors)
                assert before>=1,"Unmatched mouse release did not end the pen gesture"
                assert after==before,"Hover must not start or end a gesture"
                assert abs(angle_after-angle_before)<0.003,"Camera continued rotating without any pressed pointer"
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
