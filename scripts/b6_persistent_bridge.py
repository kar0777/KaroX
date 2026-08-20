"""B6 persistent bridge: keeps the WebSocket bridge alive between KaroX commands.

Root cause of missing overlay: when the diagnostic Python script exits, the
bridge (WebSocket server) dies. The extension's socket.onclose fires and calls
broadcastRemoveOverlay(), removing the indicator + cursor. The browser
survives (DETACHED_PROCESS) but the bridge does not.

Fix: launch the bridge as a persistent detached process that stays alive.
The extension connects to it, the overlay persists, and the user can see it.

Signal files (in runtime_dir/vnext/b6-signals/):
  - takeover   → execute takeover, write result to takeover-result.json
  - resume     → execute resume, write result to resume-result.json
  - diagnostic → run debug_overlay_state, write to diagnostic-result.json
  - stop       → gracefully stop the persistent process
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def main() -> int:
    from karox.artifacts import ArtifactStore, runtime_dir
    from karox.browser_access import BrowserAccessPolicy
    from karox.extension_browser import ChromeExtensionBrowserSessionManager

    signals_dir = Path(runtime_dir()) / "vnext" / "b6-signals"
    signals_dir.mkdir(parents=True, exist_ok=True)
    # Clean old signals
    for f in signals_dir.glob("*"):
        f.unlink()

    store = ArtifactStore("b6-live")
    policy = BrowserAccessPolicy(
        session_id="b6-live",
        external_https=True,
        headed=True,
        user_takeover=True,
        network_inspection=True,
        backend="extension",
        allowed_domains=("example.com",),
        startup_url="https://example.com/",
    )
    manager = ChromeExtensionBrowserSessionManager(store, policy)
    print("[B6-BRIDGE] reconnecting to proven managed browser...", flush=True)
    manager._ensure_started()
    inst = manager._instance
    bridge = manager._bridge
    proc = manager._process
    if inst is None or bridge is None:
        print("[B6-BRIDGE] FAIL: no instance/bridge", flush=True)
        return 1

    print(f"[B6-BRIDGE] reconnected: instance={inst.instance_id} pid={proc.pid if proc else 'reconnected'}", flush=True)
    print(f"[B6-BRIDGE] bridge port={bridge.port} hello_verified={bridge._hello_verified}", flush=True)

    # Re-apply overlay + cursor (they were removed when the old bridge died)
    print("[B6-BRIDGE] applying overlay + cursor...", flush=True)
    try:
        nav = bridge.call("navigate_to", {"url": "https://example.com/"}, 30)
        print(f"[B6-BRIDGE] navigate_to: {json.dumps(nav, default=str)}", flush=True)
    except Exception as exc:
        print(f"[B6-BRIDGE] navigate_to failed: {exc}", flush=True)
    try:
        cursor = bridge.call("show_cursor", {}, 15)
        print(f"[B6-BRIDGE] show_cursor: {json.dumps(cursor, default=str)}", flush=True)
    except Exception as exc:
        print(f"[B6-BRIDGE] show_cursor failed: {exc}", flush=True)

    # Verify state
    try:
        diag = bridge.call("debug_overlay_state", {}, 30)
        print("[B6-BRIDGE] debug_overlay_state:", flush=True)
        for k, v in diag.items():
            print(f"  {k}: {v}", flush=True)
    except Exception as exc:
        print(f"[B6-BRIDGE] debug_overlay_state failed: {exc}", flush=True)

    # Write initial status
    status_path = signals_dir / "bridge-status.json"
    status = {
        "alive": True,
        "instance_id": inst.instance_id,
        "bridge_port": bridge.port,
        "hello_verified": bridge._hello_verified,
        "pid": os.getpid(),
        "started_at": time.time(),
    }
    status_path.write_text(json.dumps(status, indent=2, default=str), encoding="utf-8")
    print(f"[B6-BRIDGE] status written to {status_path}", flush=True)
    print("[B6-BRIDGE] bridge is PERSISTENT — keeping alive...", flush=True)

    # Signal file loop: watch for commands
    while True:
        try:
            # Check for stop signal
            if (signals_dir / "stop").exists():
                print("[B6-BRIDGE] stop signal received", flush=True)
                (signals_dir / "stop").unlink(missing_ok=True)
                break

            # Check for diagnostic signal
            if (signals_dir / "diagnostic").exists():
                print("[B6-BRIDGE] diagnostic signal received", flush=True)
                (signals_dir / "diagnostic").unlink(missing_ok=True)
                try:
                    diag = bridge.call("debug_overlay_state", {}, 30)
                    result = {"ok": True, "diagnostic": diag, "at": time.time()}
                except Exception as exc:
                    result = {"ok": False, "error": str(exc), "at": time.time()}
                (signals_dir / "diagnostic-result.json").write_text(
                    json.dumps(result, indent=2, default=str), encoding="utf-8"
                )
                print("[B6-BRIDGE] diagnostic result written", flush=True)

            # Check for takeover signal
            if (signals_dir / "takeover").exists():
                print("[B6-BRIDGE] takeover signal received", flush=True)
                (signals_dir / "takeover").unlink(missing_ok=True)
                try:
                    result = bridge.call("takeover", {}, 30)
                    result = {"ok": True, "takeover": result, "at": time.time()}
                except Exception as exc:
                    result = {"ok": False, "error": str(exc), "at": time.time()}
                (signals_dir / "takeover-result.json").write_text(
                    json.dumps(result, indent=2, default=str), encoding="utf-8"
                )
                print("[B6-BRIDGE] takeover result written", flush=True)

            # Check for resume signal
            if (signals_dir / "resume").exists():
                print("[B6-BRIDGE] resume signal received", flush=True)
                (signals_dir / "resume").unlink(missing_ok=True)
                try:
                    result = bridge.call("resume", {}, 30)
                    result = {"ok": True, "resume": result, "at": time.time()}
                except Exception as exc:
                    result = {"ok": False, "error": str(exc), "at": time.time()}
                (signals_dir / "resume-result.json").write_text(
                    json.dumps(result, indent=2, default=str), encoding="utf-8"
                )
                print("[B6-BRIDGE] resume result written", flush=True)

            # Check for geometry signal
            if (signals_dir / "geometry").exists():
                print("[B6-BRIDGE] geometry signal received", flush=True)
                (signals_dir / "geometry").unlink(missing_ok=True)
                try:
                    result = bridge.call("geometry", {}, 30)
                    result = {"ok": True, "geometry": result, "at": time.time()}
                except Exception as exc:
                    result = {"ok": False, "error": str(exc), "at": time.time()}
                (signals_dir / "geometry-result.json").write_text(
                    json.dumps(result, indent=2, default=str), encoding="utf-8"
                )
                print("[B6-BRIDGE] geometry result written", flush=True)

            # Check for reload signal
            if (signals_dir / "reload").exists():
                print("[B6-BRIDGE] reload signal received", flush=True)
                (signals_dir / "reload").unlink(missing_ok=True)
                try:
                    result = bridge.call("reload_page", {}, 30)
                    result = {"ok": True, "reload": result, "at": time.time()}
                except Exception as exc:
                    result = {"ok": False, "error": str(exc), "at": time.time()}
                (signals_dir / "reload-result.json").write_text(
                    json.dumps(result, indent=2, default=str), encoding="utf-8"
                )
                print("[B6-BRIDGE] reload result written", flush=True)

            # Check for screenshot signal
            if (signals_dir / "screenshot").exists():
                print("[B6-BRIDGE] screenshot signal received", flush=True)
                (signals_dir / "screenshot").unlink(missing_ok=True)
                try:
                    result = bridge.call("screenshot", {}, 30)
                    screenshot_data = result.get("data_url") or result.get("screenshot", "")
                    if screenshot_data:
                        import base64
                        screenshot_path = signals_dir / "screenshot.png"
                        if "," in screenshot_data:
                            screenshot_data = screenshot_data.split(",", 1)[1]
                        screenshot_path.write_bytes(base64.b64decode(screenshot_data))
                        result = {"ok": True, "screenshot_path": str(screenshot_path), "at": time.time()}
                    else:
                        result = {"ok": True, "screenshot": "empty", "at": time.time()}
                except Exception as exc:
                    result = {"ok": False, "error": str(exc), "at": time.time()}
                (signals_dir / "screenshot-result.json").write_text(
                    json.dumps(result, indent=2, default=str), encoding="utf-8"
                )
                print("[B6-BRIDGE] screenshot result written", flush=True)

            # Check for tabs_all signal (ground-truth tab count)
            if (signals_dir / "tabs_all").exists():
                print("[B6-BRIDGE] tabs_all signal received", flush=True)
                (signals_dir / "tabs_all").unlink(missing_ok=True)
                try:
                    result = bridge.call("tabs_all", {}, 30)
                    result = {"ok": True, "tabs_all": result, "at": time.time()}
                except Exception as exc:
                    result = {"ok": False, "error": str(exc), "at": time.time()}
                (signals_dir / "tabs_all-result.json").write_text(
                    json.dumps(result, indent=2, default=str), encoding="utf-8"
                )
                print("[B6-BRIDGE] tabs_all result written", flush=True)

        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"[B6-BRIDGE] signal loop error: {exc}", flush=True)

        time.sleep(0.5)

    # Cleanup
    status_path.unlink(missing_ok=True)
    print("[B6-BRIDGE] shutting down", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
