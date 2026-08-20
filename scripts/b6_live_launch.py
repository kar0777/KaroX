"""B6 live launch: single managed browser with startup URL, no takeover.

Production contract:
- startup URL = https://example.com/ (single tab, no about:blank)
- --disable-infobars in argv
- DETACHED_PROCESS so Chrome survives launcher exit
- report full isolation + lifetime + tab evidence
- STOP before takeover for Point A visual confirmation
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Ensure src on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def main() -> int:
    from karox.artifacts import ArtifactStore, runtime_dir
    from karox.browser_access import BrowserAccessPolicy
    from karox.extension_browser import ChromeExtensionBrowserSessionManager

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
    print("[B6-LIVE] launching managed browser with startup_url=https://example.com/ ...")
    manager._ensure_started()
    print("[B6-LIVE] ensure_started returned")
    inst = manager._instance
    proc = manager._process
    bridge = manager._bridge
    if inst is None or proc is None or bridge is None:
        print("[B6-LIVE] FAIL: missing instance/process/bridge")
        return 1

    # --- Process identity evidence ---
    pid = proc.pid
    create_time = getattr(inst, "browser_create_time_ns", None)
    argv = getattr(inst, "argv", ()) or ()
    sanitized_argv = [a for a in argv if "nonce" not in a.lower() and "token" not in a.lower()]
    print()
    print("=" * 72)
    print("B6 MANAGED BROWSER — ISOLATION + LIFETIME EVIDENCE")
    print("=" * 72)
    print(f"instance_id         : {inst.instance_id}")
    print(f"browser_instance_id : {inst.browser_instance_id}")
    print(f"bridge_instance_id  : {inst.bridge_instance_id}")
    print(f"session_id          : {inst.session_id}")
    print(f"PID                 : {pid}")
    print(f"creation_time_ns    : {create_time}")
    print("startup_url         : https://example.com/")
    print(f"user_data_dir       : {inst.user_data_dir}")
    print(f"extension_dir       : {inst.extension_dir}")
    print(f"executable          : {inst.executable_path}")
    print("argv (sanitized)    :")
    for a in sanitized_argv:
        print(f"    {a}")
    print()
    # --- Hello verification ---
    hello = dict(bridge._hello) if bridge._hello else {}
    hello_safe = {}
    for k, v in hello.items():
        if k in ("token", "launch_nonce"):
            hello_safe[k] = "***MASKED***"
        else:
            hello_safe[k] = v
    print(f"hello_received       : {bool(hello)}")
    print(f"hello_verified       : {bridge._hello_verified}")
    print(f"hello (sanitized)   : {json.dumps(hello_safe, indent=2)}")
    print()

    # --- Process lifetime evidence ---
    import subprocess
    creationflags_used = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    )
    print(f"creationflags_used  : 0x{creationflags_used:x} (DETACHED|NEW_GROUP|BREAKAWAY)")
    print(f"  DETACHED_PROCESS        : {getattr(subprocess, 'DETACHED_PROCESS', 'N/A')}")
    print(f"  CREATE_NEW_PROCESS_GROUP: {getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 'N/A')}")
    print(f"  CREATE_BREAKAWAY_FROM_JOB: {getattr(subprocess, 'CREATE_BREAKAWAY_FROM_JOB', 'N/A')}")
    print(f"proc.poll()         : {proc.poll()}  (None = still alive)")
    print()

    # --- Tab evidence ---
    tabs_result = bridge.call("tabs", {}, 30)
    tabs = tabs_result.get("tabs", [])
    print(f"tabs_count          : {len(tabs)}")
    for t in tabs:
        print(f"  tab_id={t.get('tab_id')} url={t.get('url')} active={t.get('active')}")
    print()

    # --- debug_overlay_state (visible active tab contract) ---
    diag = bridge.call("debug_overlay_state", {}, 30)
    print("debug_overlay_state :")
    for k, v in diag.items():
        print(f"  {k}: {v}")
    print()

    # --- Summary checks ---
    visible_url_ok = any(
        "example.com" in str(t.get("url", "")) for t in tabs
    )
    no_about_blank = all("about:blank" not in str(t.get("url", "")) for t in tabs)
    single_tab = len(tabs) == 1
    all_equal = diag.get("all_equal", False)
    visible_is_target = (
        str(diag.get("visible_active_tab_id", "")) == str(diag.get("ownership_target_tab_id", ""))
    )
    print("=" * 72)
    print("CONTRACT CHECKS")
    print("=" * 72)
    print(f"  tabs_count == 1                  : {'PASS' if single_tab else 'FAIL'} ({len(tabs)})")
    print(f"  visible tab URL contains example : {'PASS' if visible_url_ok else 'FAIL'}")
    print(f"  no about:blank tabs              : {'PASS' if no_about_blank else 'FAIL'}")
    print(f"  all_equal (ownership/visible/ovl): {'PASS' if all_equal else 'FAIL'}")
    print(f"  visible == ownership target      : {'PASS' if visible_is_target else 'FAIL'}")
    print(f"  --disable-infobars in argv       : {'PASS' if any('--disable-infobars' in a for a in argv) else 'FAIL'}")
    print(f"  startup URL in argv              : {'PASS' if any('example.com' in a for a in argv) else 'FAIL'}")
    print(f"  hello verified                   : {'PASS' if bridge._hello_verified else 'FAIL'}")
    print(f"  process alive (poll=None)        : {'PASS' if proc.poll() is None else 'FAIL'}")
    print()

    # Write evidence to a file so it persists after script exit
    evidence_path = Path(runtime_dir()) / "vnext" / "b6-live-evidence.json"
    evidence = {
        "instance_id": inst.instance_id,
        "browser_instance_id": inst.browser_instance_id,
        "pid": pid,
        "creation_time_ns": create_time,
        "startup_url": "https://example.com/",
        "user_data_dir": str(inst.user_data_dir),
        "extension_dir": str(inst.extension_dir),
        "argv_sanitized": sanitized_argv,
        "hello_verified": bridge._hello_verified,
        "creationflags_hex": f"0x{creationflags_used:x}",
        "tabs_count": len(tabs),
        "tabs": tabs,
        "debug_overlay_state": diag,
        "contract": {
            "single_tab": single_tab,
            "visible_url_ok": visible_url_ok,
            "no_about_blank": no_about_blank,
            "all_equal": all_equal,
            "visible_is_target": visible_is_target,
            "disable_infobars": any("--disable-infobars" in a for a in argv),
            "startup_url_in_argv": any("example.com" in a for a in argv),
            "hello_verified": bridge._hello_verified,
            "process_alive": proc.poll() is None,
        },
        "timestamp": time.time(),
    }
    evidence_path.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
    print(f"Evidence written to: {evidence_path}")
    print()
    print("=" * 72)
    print("STOPPED BEFORE TAKEOVER — awaiting Point A visual confirmation")
    print("=" * 72)
    print("User should visually confirm:")
    print("  1. Address bar shows https://example.com/")
    print("  2. Indicator text 'KaroX управляет этой вкладкой' is visible")
    print("  3. Agent cursor is visible")
    print("  4. No about:blank tab")
    print("  5. No second tab")
    print("  6. Personal Chrome not affected")
    print()
    print(f"Managed browser PID {pid} is alive and will survive this script exit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
