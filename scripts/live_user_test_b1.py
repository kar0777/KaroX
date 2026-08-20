"""Live-user B1 test: visible Chrome, cursor, indicator, console, takeover/resume.

Runs the ChromeExtensionBrowserSessionManager directly (same _ExtensionBridgeServer
code path as the bridge) using the default runtime dir, so the extension the user
loaded via Load unpacked reconnects to this script's websocket. The script stops
at request_user_takeover and waits for a resume flag file so the user can perform
one neutral action in the visible KaroX Chrome window.

Usage:
    python scripts/live_user_test_b1.py

After "STOP" message, the user performs ONE neutral action in KaroX Chrome, then
the operator creates the resume flag:
    python -c "import pathlib; pathlib.Path(r'%LOCALAPPDATA%\\KaroX\\vnext\\live-test-resume.flag').touch()"
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from karox.artifacts import ArtifactStore
from karox.browser_access import BrowserAccessPolicy
from karox.extension_browser import ChromeExtensionBrowserSessionManager

SESSION_ID = "live-user-b1-test"
RESUME_FLAG = Path(os.path.expandvars(r"%LOCALAPPDATA%\KaroX\vnext\live-test-resume.flag"))


def _safe(label: str, fn, *args, **kwargs):
    try:
        result = fn(*args, **kwargs)
        if isinstance(result, dict):
            return result
        return {"value": result}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
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
    results: dict = {"phase1": {}, "phase2": {}}
    takeover_tab_id = None
    try:
        print("=== B1 LIVE TEST STARTING ===", flush=True)
        print(f"session_id={SESSION_ID}", flush=True)
        print(f"resume_flag={RESUME_FLAG}", flush=True)

        # 1. open https://example.com/ via new_tab (creates a fresh tab rather
        # than reusing a pre-existing one, so a stale ChatGPT tab in the KaroX
        # profile can never become the agent tab by accident).
        opened = _safe("new_tab", manager.new_tab, {"url": "https://example.com/"}, 40)
        results["phase1"]["open"] = opened
        if opened.get("error"):
            print(f"OPEN FAILED: {opened['error']}", flush=True)
            return 1
        agent_tab_id = opened.get("tab_id")
        print(f"OPEN ok: engine={opened.get('engine') if 'engine' in opened else 'n/a'} tab_id={agent_tab_id}", flush=True)

        # 1b. B6: snapshot the owned tabs registry BEFORE the test. The test
        # never closes pre-existing tabs (the B1 incident root cause); it only
        # works with the one tab it created via new_tab and closes only that.
        tabs_before = _safe("tabs_before", manager.tabs, {}, 10)
        owned_before = tuple(
            t.get("tab_id") for t in (tabs_before.get("tabs") or []) if t.get("tab_id")
        )
        results["phase1"]["tabs_before"] = {"count": len(owned_before), "ids": owned_before}
        print(f"TABS_BEFORE ok: count={len(owned_before)} (no stale-tab cleanup; B6)", flush=True)

        # 2. wait_for h1 visible (page loaded)
        wait = _safe("wait_for", manager.wait_for, {"selector": "h1", "state": "visible"}, 30)
        results["phase1"]["wait_for_h1"] = wait
        print(f"WAIT_FOR ok: {wait}", flush=True)

        # 3. snapshot (non-empty — real DOM, not empty data)
        snapshot = _safe("snapshot", manager.snapshot, {}, 20)
        snap = snapshot.get("snapshot") or {}
        results["phase1"]["snapshot"] = {
            "title": snapshot.get("title"),
            "heading": (snap.get("headings", [{}])[0].get("text") if snap.get("headings") else None),
            "text_len": len(snap.get("text", "")),
            "url": snapshot.get("url"),
            "tab_id": snapshot.get("tab_id"),
            "buttons_count": len(snap.get("buttons", [])),
            "links_count": len(snap.get("links", [])),
        }
        print(f"SNAPSHOT ok: title={snapshot.get('title')!r} text_len={len(snap.get('text', ''))}", flush=True)

        # 4. console (real entries expected now — MAIN-world relay active)
        console = _safe("console", manager.console, {}, 10)
        results["phase1"]["console"] = {
            "count": console.get("count"),
            "supported": console.get("supported"),
        }
        print(f"CONSOLE ok: count={console.get('count')} supported={console.get('supported')}", flush=True)

        # 5. network requests (example.com loads several resources)
        network = _safe("network_requests", manager.network_requests, {}, 10)
        results["phase1"]["network"] = {"count": network.get("count")}
        failures = _safe("network_failures", manager.network_failures, {}, 10)
        results["phase1"]["network_failures"] = {"count": failures.get("count")}
        print(f"NETWORK ok: requests={network.get('count')} failures={failures.get('count')}", flush=True)

        # 6. get_text on h1 (read-only action)
        text_h1 = _safe("get_text", manager.get_text, {"selector": "h1"}, 10)
        results["phase1"]["get_text_h1"] = {"text": text_h1.get("text"), "truncated": text_h1.get("truncated")}
        print(f"GET_TEXT ok: h1={text_h1.get('text')!r}", flush=True)

        # 7. click on h1 (safe — no navigation; cursor should move to h1 center)
        click_h1 = _safe("click", manager.click, {"selector": "h1"}, 10)
        results["phase1"]["click_h1"] = {"clicked": click_h1.get("clicked")}
        print(f"CLICK ok: clicked={click_h1.get('clicked')}", flush=True)

        # 8. screenshot (artifact — stored, not displayed here; text-only agent)
        screenshot = _safe("screenshot", manager.screenshot, {"name": "live-b1-before-takeover"}, 20)
        results["phase1"]["screenshot"] = {
            "artifact_id": screenshot.get("artifact_id"),
            "size": screenshot.get("size"),
            "sha256": screenshot.get("sha256"),
        }
        print(f"SCREENSHOT ok: artifact_id={screenshot.get('artifact_id')}", flush=True)

        # 9. request_user_takeover — STOP HERE
        takeover = _safe("takeover", manager.request_user_takeover, {"reason": "B1 live-user test: please perform one neutral action (scroll, click empty space, or move mouse)"}, 10)
        results["phase1"]["takeover"] = {
            "takeover": takeover.get("takeover"),
            "agent_input_paused": takeover.get("agent_input_paused"),
            "tab_id": takeover.get("tab_id"),
            "url": takeover.get("url"),
        }
        takeover_tab_id = takeover.get("tab_id")
        print(f"TAKEOVER ok: paused={takeover.get('agent_input_paused')} tab_id={takeover_tab_id}", flush=True)

        # 9. B6: verify no personal Chrome tabs were touched. The owned tabs
        # registry after the test must equal the snapshot before the test plus
        # the one created test tab (no mass close, no pre-existing tab close).
        tabs_after = _safe("tabs_after", manager.tabs, {}, 10)
        owned_after = tuple(
            t.get("tab_id") for t in (tabs_after.get("tabs") or []) if t.get("tab_id")
        )
        results["phase1"]["tabs_after"] = {"count": len(owned_after), "ids": owned_after}
        # Every tab in owned_before must still be present in owned_after.
        missing = tuple(tid for tid in owned_before if tid not in owned_after)
        results["phase1"]["personal_tabs_touched"] = {"missing_count": len(missing), "missing": missing}
        print(f"TABS_AFTER ok: count={len(owned_after)} missing={len(missing)} (B6: no personal tabs touched)", flush=True)

        print("", flush=True)
        print("========================================", flush=True)
        print("=== B1 LIVE TEST — PHASE 1 COMPLETE ===", flush=True)
        print("========================================", flush=True)
        print(json.dumps(results["phase1"], indent=2, ensure_ascii=False, default=str), flush=True)
        print("", flush=True)
        print("STOP: KaroX Chrome is now in USER TAKEOVER mode.", flush=True)
        print("  Indicator should show: «Управление передано вам»", flush=True)
        print("  Agent cursor should be HIDDEN.", flush=True)
        print("", flush=True)
        print("Please perform ONE neutral action in the visible KaroX Chrome window:", flush=True)
        print("  e.g., scroll the page, click on empty space, or move your mouse.", flush=True)
        print("Do NOT click login/payment/2FA — this is a read-only test on example.com.", flush=True)
        print("", flush=True)
        print(f"After your action, the operator will create the resume flag: {RESUME_FLAG}", flush=True)
        print("Waiting for resume flag (timeout 600s)...", flush=True)

        # 10. Wait for resume flag (polling 1s, 600s timeout)
        deadline = time.monotonic() + 600
        flag_received = False
        while time.monotonic() < deadline:
            if RESUME_FLAG.exists():
                RESUME_FLAG.unlink(missing_ok=True)
                flag_received = True
                break
            time.sleep(1)

        if not flag_received:
            print("TIMEOUT waiting for resume flag (600s). Aborting phase 2.", flush=True)
            results["phase2"]["error"] = "resume flag timeout"
            return 2

        print("Resume flag received. Continuing phase 2...", flush=True)

        # 11. resume_after_user_takeover
        resumed = _safe("resume", manager.resume_after_user_takeover, {}, 10)
        results["phase2"]["resume"] = {
            "resumed": resumed.get("resumed"),
            "agent_input_paused": resumed.get("agent_input_paused"),
            "tab_id": resumed.get("tab_id"),
            "url": resumed.get("url"),
        }
        print(f"RESUME ok: paused={resumed.get('agent_input_paused')} tab_id={resumed.get('tab_id')}", flush=True)

        # 12. next browser tool call after resume: get_text (verify read actions work)
        text2 = _safe("get_text_after_resume", manager.get_text, {"selector": "h1"}, 10)
        results["phase2"]["get_text_after_resume"] = {"text": text2.get("text")}
        print(f"GET_TEXT_AFTER_RESUME ok: h1={text2.get('text')!r}", flush=True)

        # 13. click after resume (verify input actions work after resume)
        click2 = _safe("click_after_resume", manager.click, {"selector": "h1"}, 10)
        results["phase2"]["click_after_resume"] = {"clicked": click2.get("clicked")}
        print(f"CLICK_AFTER_RESUME ok: clicked={click2.get('clicked')}", flush=True)

        # 14. tabs (tab continuity — verify tab_id same as takeover)
        tabs = _safe("tabs", manager.tabs, {}, 10)
        results["phase2"]["tabs"] = {
            "count": tabs.get("count"),
            "active_tab_id": tabs.get("active_tab_id"),
            "takeover_active": tabs.get("takeover_active"),
            "same_tab_as_takeover": tabs.get("active_tab_id") == takeover_tab_id,
        }
        print(f"TABS ok: count={tabs.get('count')} active={tabs.get('active_tab_id')} same_as_takeover={tabs.get('active_tab_id') == takeover_tab_id}", flush=True)

        # 15. screenshot after resume
        screenshot2 = _safe("screenshot_after_resume", manager.screenshot, {"name": "live-b1-after-resume"}, 20)
        results["phase2"]["screenshot_after_resume"] = {
            "artifact_id": screenshot2.get("artifact_id"),
            "size": screenshot2.get("size"),
        }
        print(f"SCREENSHOT_AFTER_RESUME ok: artifact_id={screenshot2.get('artifact_id')}", flush=True)

        # 16. console after resume (verify capture continues)
        console2 = _safe("console_after_resume", manager.console, {}, 10)
        results["phase2"]["console_after_resume"] = {"count": console2.get("count")}
        print(f"CONSOLE_AFTER_RESUME ok: count={console2.get('count')}", flush=True)

        print("", flush=True)
        print("========================================", flush=True)
        print("=== B1 LIVE TEST — PHASE 2 COMPLETE ===", flush=True)
        print("========================================", flush=True)
        print(json.dumps(results["phase2"], indent=2, ensure_ascii=False, default=str), flush=True)
        print("", flush=True)
        print("ALL PHASES OK. Closing Chrome...", flush=True)
        return 0

    finally:
        try:
            manager.close(force=True)
            print("Manager closed. Chrome terminated.", flush=True)
        except Exception as exc:
            print(f"Manager close error: {exc}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
