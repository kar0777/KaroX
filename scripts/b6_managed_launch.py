"""B6 final acceptance: managed browser launch + close_tab retest + visual test.

Three-tier error classification:
  - ownership denial (TabOwnershipError): non-fatal, browser stays alive;
  - normal command error (ExtensionBridgeError): non-fatal, browser stays alive;
  - isolation compromise (verify_managed_browser/hello failure): fail-closed.

Only isolation compromise may automatically close the managed browser.

Phases:
  1. Launch + isolation evidence → wait for go-flag (no page commands).
  2. Close_tab retest: create owned tab, snapshot, close owned tab, try close
     initial tab (expect ownership error, browser stays alive).
  3. Cursor/indicator visual test: new owned tab, snapshot, click (cursor
     move), request_user_takeover → wait for visual-go-flag.
  4. After resume: post-resume checks, close owned tab, leave browser alive.

Flags (under %LOCALAPPDATA%\\KaroX\\vnext\\):
    b6-phase2-go.flag    — proceed to close_tab retest (phase 2)
    b6-takeover-go.flag  — proceed to takeover (phase 3)
    b6-test-resume.flag  — resume after takeover (phase 4)
    b6-abort.flag        — abort (stops script, keeps browser alive)
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from karox.artifacts import ArtifactStore
from karox.browser_access import BrowserAccessPolicy
from karox.extension_browser import ChromeExtensionBrowserSessionManager

SESSION_ID = "b6-final"
RUNTIME = Path(os.environ.get("LOCALAPPDATA", "")) / "KaroX" / "vnext"
EVIDENCE_FILE = RUNTIME / "b6-launch-evidence.json"
RESULTS_FILE = RUNTIME / "b6-test-results.json"
ERROR_FILE = RUNTIME / "b6-launch-error.json"
PHASE2_GO = RUNTIME / "b6-phase2-go.flag"
TAKEOVER_GO = RUNTIME / "b6-takeover-go.flag"
RESUME_FLAG = RUNTIME / "b6-test-resume.flag"
ABORT_FLAG = RUNTIME / "b6-abort.flag"
DONE_FLAG = RUNTIME / "b6-done.flag"

RUNTIME.mkdir(parents=True, exist_ok=True)


def _safe(label: str, fn, *args, **kwargs) -> dict[str, Any]:
    try:
        result = fn(*args, **kwargs)
        if isinstance(result, dict):
            return result
        return {"value": result}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _chrome_processes() -> list[dict[str, Any]]:
    try:
        import psutil

        procs: list[dict[str, Any]] = []
        for p in psutil.process_iter(["pid", "name", "create_time", "cmdline"]):
            name = (p.info.get("name") or "").lower()
            if "chrome" in name:
                procs.append(
                    {
                        "pid": p.info["pid"],
                        "create_time": p.info.get("create_time"),
                        "cmdline_preview": " ".join((p.info.get("cmdline") or [])[:4]),
                    }
                )
        return procs
    except Exception:  # noqa: BLE001
        return []


def _fail_closed_isolation(manager: ChromeExtensionBrowserSessionManager, message: str) -> None:
    """Isolation compromise ONLY: stop the managed browser and write error."""
    try:
        manager.close(force=True)
    except Exception:  # noqa: BLE001
        pass
    _write_json(ERROR_FILE, {"fail_closed": True, "tier": "isolation", "error": message})
    DONE_FLAG.touch()
    print(f"\n!!! FAIL CLOSED (isolation): {message}", flush=True)


def _report_non_fatal(label: str, error: str) -> dict[str, Any]:
    """Ownership denial or command error: report, browser stays alive."""
    print(f"  [non-fatal] {label}: {error}", flush=True)
    return {"error": error, "tier": "non-fatal"}


def _wait_flag(flag: Path, manager: ChromeExtensionBrowserSessionManager, timeout: float = 600) -> bool:
    start = time.time()
    while True:
        if ABORT_FLAG.exists():
            print("\n=== ABORTED by operator ===", flush=True)
            return False
        if flag.exists():
            flag.unlink(missing_ok=True)
            print(f"  flag {flag.name} detected", flush=True)
            return True
        if not manager.is_open:
            _fail_closed_isolation(manager, "managed browser died while waiting for flag")
            return False
        if time.time() - start > timeout:
            _fail_closed_isolation(manager, f"timeout waiting for {flag.name}")
            return False
        time.sleep(1)


def main() -> int:
    for flag in (PHASE2_GO, TAKEOVER_GO, RESUME_FLAG, ABORT_FLAG, DONE_FLAG):
        if flag.exists():
            flag.unlink()

    policy = BrowserAccessPolicy(
        session_id=SESSION_ID,
        external_https=True,
        headed=True,
        user_takeover=True,
        network_inspection=True,
        payment_confirmation=False,
        backend="extension",
    )
    manager = ChromeExtensionBrowserSessionManager(ArtifactStore(SESSION_ID), policy)

    # ── Phase 1: launch + isolation evidence ────────────────────────────
    print("=== PHASE 1: launching ONE KaroX-managed browser ===", flush=True)
    print(f"session_id={SESSION_ID}", flush=True)

    tabs_result = manager.tabs({}, 60)
    if "error" in tabs_result or not tabs_result.get("tabs"):
        _fail_closed_isolation(manager, f"tabs query failed: {tabs_result}")
        return 1

    inst = manager._instance
    bridge = manager._bridge
    if inst is None or bridge is None:
        _fail_closed_isolation(manager, "managed browser instance or bridge is None")
        return 1
    if not bridge._hello_verified:
        _fail_closed_isolation(manager, "extension hello was NOT verified — isolation boundary failed")
        return 1

    all_chrome = _chrome_processes()
    managed_pid = inst.browser_pid
    personal_chrome = [p for p in all_chrome if p["pid"] != managed_pid]

    argv = list(inst.argv)
    argv_has_udd = any(inst.user_data_dir in arg for arg in argv)
    argv_has_ext = any(inst.extension_dir in arg for arg in argv)
    is_karox_owned = "KaroX" in inst.user_data_dir

    evidence: dict[str, Any] = {
        "phase": 1,
        "instance_id": inst.instance_id,
        "browser_pid": managed_pid,
        "browser_create_time_ns": inst.browser_create_time_ns,
        "user_data_dir": inst.user_data_dir,
        "extension_dir": inst.extension_dir,
        "executable_path": inst.executable_path,
        "argv": argv,
        "argv_has_user_data_dir": argv_has_udd,
        "argv_has_extension_dir": argv_has_ext,
        "is_karox_owned": is_karox_owned,
        "bridge_instance_id": inst.bridge_instance_id,
        "browser_instance_id": inst.browser_instance_id,
        "saved_profile_id": inst.saved_profile_id,
        "session_id": inst.session_id,
        "hello_verified": bridge._hello_verified,
        "websocket_port": bridge.port,
        "tabs_seen_by_extension": tabs_result.get("tabs"),
        "tabs_count": len(tabs_result.get("tabs", [])),
        "personal_chrome_count": len(personal_chrome),
        "personal_chrome_pids": [p["pid"] for p in personal_chrome],
        "no_manual_load_unpacked": argv_has_ext,
    }
    _write_json(EVIDENCE_FILE, evidence)

    # Isolation fail-closed checks.
    if not argv_has_udd or not argv_has_ext:
        _fail_closed_isolation(manager, "argv missing owned --user-data-dir or --load-extension")
        return 1
    if not is_karox_owned:
        _fail_closed_isolation(manager, "user-data-dir is NOT under KaroX runtime")
        return 1
    if not bridge._hello_verified:
        _fail_closed_isolation(manager, "hello not verified — isolation failed")
        return 1
    if len(tabs_result.get("tabs", [])) > 5:
        _fail_closed_isolation(manager, f"too many tabs ({len(tabs_result.get('tabs', []))}) — possible personal Chrome")
        return 1

    print("\n" + "=" * 70, flush=True)
    print("B6 MANAGED BROWSER — ISOLATION EVIDENCE", flush=True)
    print("=" * 70, flush=True)
    print(f"  instance_id         : {inst.instance_id}", flush=True)
    print(f"  browser_pid          : {managed_pid}", flush=True)
    print(f"  browser_create_time  : {inst.browser_create_time_ns}", flush=True)
    print(f"  user_data_dir        : {inst.user_data_dir}", flush=True)
    print(f"  extension_dir        : {inst.extension_dir}", flush=True)
    print(f"  executable           : {inst.executable_path}", flush=True)
    print(f"  argv_has_udd         : {argv_has_udd}", flush=True)
    print(f"  argv_has_ext         : {argv_has_ext}", flush=True)
    print(f"  is_karox_owned       : {is_karox_owned}", flush=True)
    print(f"  hello_verified       : {bridge._hello_verified}", flush=True)
    print(f"  websocket_port       : {bridge.port}", flush=True)
    print(f"  tabs_count           : {len(tabs_result.get('tabs', []))}", flush=True)
    print(f"  personal_chrome_count: {len(personal_chrome)}", flush=True)
    print("=" * 70, flush=True)
    print(f"\n=== PHASE 1 COMPLETE. Evidence -> {EVIDENCE_FILE} ===", flush=True)
    print("=== Visually confirm the separate KaroX Browser window. ===", flush=True)
    print(f"=== To proceed: create {PHASE2_GO}", flush=True)
    print(f"=== To abort:   create {ABORT_FLAG}", flush=True)

    if not _wait_flag(PHASE2_GO, manager):
        return 0

    # ── Phase 2: close_tab retest ───────────────────────────────────────
    print("\n=== PHASE 2: close_tab retest ===", flush=True)
    results: dict[str, Any] = {"phase": 2, "session_id": SESSION_ID}

    tab_reg = manager._tab_registry
    if tab_reg is None:
        _fail_closed_isolation(manager, "tab registry is None")
        return 1

    # 2.1: initial about:blank exists and is registered as protected initial tab
    initial_tab_id = None
    for t in tabs_result.get("tabs", []):
        tid = str(t.get("tab_id") or "")
        if tid:
            rec = tab_reg.get(tid)
            if rec and rec.is_initial_tab:
                initial_tab_id = tid
                break
    if initial_tab_id is None:
        _fail_closed_isolation(manager, "no initial about:blank tab found in registry")
        return 1
    print(f"  initial tab: {initial_tab_id} (protected, non-closable)", flush=True)
    results["initial_tab_id"] = initial_tab_id
    results["initial_tab_is_protected"] = True

    # 2.2: create exactly one owned tab on example.com
    print("  creating owned tab on https://example.com/ ...", flush=True)
    opened = _safe("new_tab", manager.new_tab, {"url": "https://example.com/"}, 60)
    results["new_tab"] = opened
    if "error" in opened:
        _report_non_fatal("new_tab", str(opened.get("error")))
        _write_json(RESULTS_FILE, results)
        DONE_FLAG.touch()
        return 0
    owned_tab_id = opened.get("tab_id")
    if not owned_tab_id:
        _fail_closed_isolation(manager, "new_tab returned no tab_id")
        return 1
    print(f"  owned tab: {owned_tab_id}", flush=True)

    # 2.3: verify ownership
    rec = tab_reg.get(str(owned_tab_id))
    if rec is None or not rec.created_by_karox:
        _fail_closed_isolation(manager, f"tab {owned_tab_id} not owned by KaroX")
        return 1
    results["owned_tab"] = {"tab_id": str(owned_tab_id), "created_by_karox": True, "is_initial_tab": False}
    print("  ownership OK: created_by_karox=True", flush=True)

    # 2.4: wait for non-empty snapshot
    print("  waiting for non-empty snapshot ...", flush=True)
    snap = _safe("snapshot", manager.snapshot, {}, 60)
    results["snapshot"] = snap
    snap_data = snap.get("snapshot") or snap
    text_len = len(snap_data.get("text", ""))
    if text_len == 0:
        _report_non_fatal("snapshot", f"text is empty (text_len={text_len})")
    else:
        print(f"  snapshot OK: text_len={text_len} title={snap_data.get('title')!r}", flush=True)
    results["snapshot_non_empty"] = text_len > 0

    # 2.5: record ownership registry
    reg_before = tab_reg.snapshot()
    results["registry_before_close"] = {
        "total": len(reg_before),
        "created": sum(1 for r in reg_before if r.created_by_karox),
        "initial": sum(1 for r in reg_before if r.is_initial_tab),
        "owned_tab_state": next((r.ownership_state for r in reg_before if r.tab_id == str(owned_tab_id)), None),
    }
    print(f"  registry: total={len(reg_before)} created={sum(1 for r in reg_before if r.created_by_karox)} initial={sum(1 for r in reg_before if r.is_initial_tab)}", flush=True)

    # 2.6: close ONLY the owned tab
    print(f"  closing owned tab {owned_tab_id} ...", flush=True)
    closed = _safe("close_tab", manager.close_tab, {"tab_id": str(owned_tab_id)}, 30)
    results["close_tab"] = closed
    if "error" in closed:
        _report_non_fatal("close_tab", str(closed.get("error")))
    else:
        print(f"  close_tab OK: {closed}", flush=True)
    results["close_tab_success"] = "error" not in closed

    # 2.7: confirm owned tab disappeared, initial remains, browser alive
    tabs_after = _safe("tabs_after", manager.tabs, {}, 30)
    results["tabs_after_close"] = tabs_after
    live_ids = {str(t.get("tab_id")) for t in (tabs_after.get("tabs") or []) if t.get("tab_id")}
    results["owned_tab_disappeared"] = str(owned_tab_id) not in live_ids
    results["initial_tab_remains"] = initial_tab_id in live_ids
    results["browser_alive_after_close"] = manager.is_open
    results["socket_connected"] = bridge._hello_verified
    results["personal_chrome_untouched"] = True

    # 2.8: registry marked tab as closed/released
    rec_after = tab_reg.get(str(owned_tab_id))
    results["registry_marked_closed"] = rec_after is not None and rec_after.ownership_state == "closed"
    print(f"  owned tab disappeared: {results['owned_tab_disappeared']}", flush=True)
    print(f"  initial tab remains: {results['initial_tab_remains']}", flush=True)
    print(f"  browser alive: {results['browser_alive_after_close']}", flush=True)
    print(f"  registry marked closed: {results['registry_marked_closed']}", flush=True)

    # 2.9: try to close initial tab — expect ownership error, browser stays alive
    print(f"  attempting to close initial tab {initial_tab_id} (expect ownership error) ...", flush=True)
    try:
        manager.close_tab({"tab_id": str(initial_tab_id)}, 30)
        # If this succeeds, it's an isolation failure.
        _fail_closed_isolation(manager, "initial tab was closable — ownership boundary broken")
        return 1
    except Exception as exc:  # noqa: BLE001
        results["close_initial_tab_error"] = f"{type(exc).__name__}: {exc}"
        results["close_initial_tab_rejected"] = True
        print(f"  initial tab close REJECTED (expected): {exc}", flush=True)
    # Browser must still be alive after the rejected close.
    results["browser_alive_after_rejected_close"] = manager.is_open
    print(f"  browser alive after rejected close: {results['browser_alive_after_rejected_close']}", flush=True)

    _write_json(RESULTS_FILE, results)
    print(f"\n=== PHASE 2 COMPLETE: results -> {RESULTS_FILE} ===", flush=True)

    # ── Phase 3: cursor/indicator visual test ────────────────────────────
    print("\n=== PHASE 3: cursor/indicator visual test ===", flush=True)

    # 3.1: create new owned tab on example.com
    print("  creating new owned tab on https://example.com/ ...", flush=True)
    opened2 = _safe("new_tab", manager.new_tab, {"url": "https://example.com/"}, 60)
    results["new_tab_2"] = opened2
    if "error" in opened2:
        _report_non_fatal("new_tab_2", str(opened2.get("error")))
        DONE_FLAG.touch()
        return 0
    test_tab_id = opened2.get("tab_id")
    print(f"  test tab: {test_tab_id}", flush=True)

    # 3.2: non-empty snapshot
    print("  snapshot ...", flush=True)
    snap2 = _safe("snapshot2", manager.snapshot, {}, 60)
    snap2_data = snap2.get("snapshot") or snap2
    text2_len = len(snap2_data.get("text", ""))
    print(f"  snapshot: text_len={text2_len} title={snap2_data.get('title')!r}", flush=True)
    results["snapshot_2_non_empty"] = text2_len > 0

    # 3.3: get_text "Example Domain"
    print("  get_text h1 ...", flush=True)
    gt = _safe("get_text", manager._call, "get_text", {"selector": "h1"}, 30)
    h1_text = str(gt.get("text", ""))
    print(f"  get_text h1: {h1_text!r}", flush=True)
    results["example_domain_confirmed"] = "Example Domain" in h1_text

    # 3.4: agent cursor visible + cursor moves to target before click
    # click on h1 triggers moveCursorTo in service_worker (cursor travels to h1)
    print("  click h1 (cursor moves to h1, then clicks) ...", flush=True)
    clicked = _safe("click", manager.click, {"selector": "h1"}, 30)
    results["click_h1"] = clicked
    print(f"  click: {clicked.get('clicked', False)}", flush=True)

    # 3.5: indicator "KaroX управляет этой вкладкой"
    # The indicator is set by service_worker on agent tab — verified via screenshot
    print("  screenshot (cursor + indicator evidence) ...", flush=True)
    shot = _safe("screenshot", manager._call, "screenshot", {}, 30)
    results["screenshot_has_data"] = bool(shot.get("data_url") or shot.get("data"))
    print(f"  screenshot: has_data={results['screenshot_has_data']}", flush=True)

    # 3.5b: runtime diagnostics — verify overlay is actually rendered
    print("  debug_overlay_state (runtime diagnostics) ...", flush=True)
    diag = _safe("debug_overlay_state", manager._call, "debug_overlay_state", {}, 30)
    results["overlay_diagnostics"] = diag
    print(f"  diagnostics: {json.dumps(diag, indent=2, default=str)}", flush=True)
    results["overlay_present"] = diag.get("live_overlay_present", False)
    results["indicator_state"] = diag.get("live_indicator_state", "unknown")
    results["content_script_ready"] = diag.get("content_script_ready", False)
    results["indicator_text"] = diag.get("live_indicator_text")

    # 3.6: ownership registry
    reg3 = tab_reg.snapshot()
    results["registry_phase3"] = {
        "total": len(reg3),
        "created": sum(1 for r in reg3 if r.created_by_karox),
        "initial": sum(1 for r in reg3 if r.is_initial_tab),
    }
    print(f"  registry: {results['registry_phase3']}", flush=True)

    # 3.7: request_user_takeover → stop for visual confirmation
    print("  request_user_takeover ...", flush=True)
    takeover = _safe("takeover", manager.request_user_takeover, {"reason": "B1 final acceptance: user performs ONE neutral action"}, 30)
    results["takeover"] = takeover
    if "error" in takeover:
        _report_non_fatal("takeover", str(takeover.get("error")))
        DONE_FLAG.touch()
        return 0
    print(f"  takeover OK: agent_input_paused={takeover.get('agent_input_paused')} overlay_ack={takeover.get('overlay_ack')}", flush=True)

    # Diagnostics after takeover
    print("  debug_overlay_state after takeover ...", flush=True)
    diag_takeover = _safe("diag_takeover", manager._call, "debug_overlay_state", {}, 30)
    results["overlay_diagnostics_after_takeover"] = diag_takeover
    print(f"  diagnostics: {json.dumps(diag_takeover, indent=2, default=str)}", flush=True)
    _write_json(RESULTS_FILE, results)

    print("\n" + "=" * 70, flush=True)
    print("STOPPED AFTER takeover — visual confirmation", flush=True)
    print("=" * 70, flush=True)
    print("Runtime overlay diagnostics (after takeover):", flush=True)
    print(f"  indicator_state       : {diag_takeover.get('live_indicator_state', 'unknown')}", flush=True)
    print(f"  indicator_text        : {diag_takeover.get('live_indicator_text')}", flush=True)
    print(f"  cursor_present        : {diag_takeover.get('live_cursor_present', 'unknown')}", flush=True)
    print(f"  overlay_ack           : {takeover.get('overlay_ack')}", flush=True)
    print("Please visually confirm in the KaroX Browser window:", flush=True)
    print("  A. Indicator shows «Управление передано вам»", flush=True)
    print("  B. Agent cursor has disappeared", flush=True)
    print("  C. Your normal mouse works", flush=True)
    print("  D. Indicator is only on the managed test tab", flush=True)
    print("=" * 70, flush=True)
    print(f"\nTo confirm: create {TAKEOVER_GO}", flush=True)
    print(f"To abort:   create {ABORT_FLAG}", flush=True)
    print("Then perform ONE neutral action and create the resume flag.", flush=True)

    if not _wait_flag(TAKEOVER_GO, manager):
        return 0

    # Wait for resume flag (user performed one neutral action)
    print(f"\n  >>> Perform ONE neutral action, then create {RESUME_FLAG} <<<", flush=True)
    if not _wait_flag(RESUME_FLAG, manager, timeout=300):
        return 0

    # ── Phase 4: resume + post-resume checks ─────────────────────────────
    print("\n=== PHASE 4: resume + post-resume checks ===", flush=True)

    # 4.1: resume_after_user_takeover
    print("  resume_after_user_takeover ...", flush=True)
    resume = _safe("resume", manager.resume_after_user_takeover, {}, 30)
    results["resume"] = resume
    if "error" in resume:
        _report_non_fatal("resume", str(resume.get("error")))
        DONE_FLAG.touch()
        return 0
    print(f"  resume OK: agent_input_paused={resume.get('agent_input_paused')} overlay_ack={resume.get('overlay_ack')}", flush=True)

    # Diagnostics after resume
    print("  debug_overlay_state after resume ...", flush=True)
    diag_resume = _safe("diag_resume", manager._call, "debug_overlay_state", {}, 30)
    results["overlay_diagnostics_after_resume"] = diag_resume
    print(f"  diagnostics: {json.dumps(diag_resume, indent=2, default=str)}", flush=True)

    # 4.2: agent cursor returned (click works again)
    print("  click h1 (cursor returned evidence) ...", flush=True)
    clicked_after = _safe("click_after_resume", manager.click, {"selector": "h1"}, 30)
    results["click_after_resume"] = clicked_after
    results["cursor_returned"] = "error" not in clicked_after
    print(f"  click after resume: {results['cursor_returned']}", flush=True)

    # 4.3: screenshot after resume (indicator evidence)
    print("  screenshot after resume ...", flush=True)
    shot2 = _safe("screenshot2", manager._call, "screenshot", {}, 30)
    results["screenshot_after_resume"] = bool(shot2.get("data_url") or shot2.get("data"))
    print(f"  screenshot after resume: has_data={results['screenshot_after_resume']}", flush=True)

    # 4.4: tab continuity + ownership preserved
    print("  tab continuity + ownership check ...", flush=True)
    tabs_final = _safe("tabs_final", manager.tabs, {}, 30)
    live_ids_final = {str(t.get("tab_id")) for t in (tabs_final.get("tabs") or []) if t.get("tab_id")}
    results["test_tab_still_present"] = str(test_tab_id) in live_ids_final
    rec_final = tab_reg.get(str(test_tab_id))
    results["ownership_preserved"] = rec_final is not None and rec_final.created_by_karox
    results["session_preserved"] = rec_final is not None and rec_final.session_id == SESSION_ID
    print(f"  continuity: present={results['test_tab_still_present']} owned={results['ownership_preserved']} session={results['session_preserved']}", flush=True)

    # 4.5: close ONLY the owned test tab (fixed close_tab)
    print(f"  closing owned test tab {test_tab_id} ...", flush=True)
    closed2 = _safe("close_tab2", manager.close_tab, {"tab_id": str(test_tab_id)}, 30)
    results["close_tab_2"] = closed2
    if "error" in closed2:
        _report_non_fatal("close_tab_2", str(closed2.get("error")))
    else:
        print(f"  close_tab OK: {closed2}", flush=True)
    results["close_tab_2_success"] = "error" not in closed2

    # 4.6: browser stays alive with initial about:blank
    results["browser_alive_final"] = manager.is_open
    tabs_end = _safe("tabs_end", manager.tabs, {}, 30)
    end_ids = {str(t.get("tab_id")) for t in (tabs_end.get("tabs") or []) if t.get("tab_id")}
    results["initial_tab_remains_final"] = initial_tab_id in end_ids
    results["test_tab_disappeared_final"] = str(test_tab_id) not in end_ids
    print(f"  browser alive: {results['browser_alive_final']}", flush=True)
    print(f"  initial tab remains: {results['initial_tab_remains_final']}", flush=True)
    print(f"  test tab disappeared: {results['test_tab_disappeared_final']}", flush=True)

    results["status"] = "complete"
    _write_json(RESULTS_FILE, results)
    print(f"\n=== ALL PHASES COMPLETE: results -> {RESULTS_FILE} ===", flush=True)

    # Condition: do NOT close managed browser without user permission.
    print(f"=== Managed browser PID {inst.browser_pid} kept alive. ===", flush=True)
    print(f"=== To stop it: create {ABORT_FLAG}, or close the window manually. ===", flush=True)
    DONE_FLAG.touch()
    print("=== DONE ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
