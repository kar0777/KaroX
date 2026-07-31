#!/usr/bin/env python3
"""Browser audit of the Vacancy Control localhost UI through the real KaroX
HostedToolsRuntime. Mirrors the smoke-test login flow (local workspace gate,
NOT Facebook login) and walks each screen, capturing snapshot/console/
network_failures/screenshot/artifact.read_image per screen.

No publish/autopost/autopilot/warmup/bump/join-group/like/comment/
Facebook-login/database-reset.
"""
import sys, os, tempfile, time, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from pathlib import Path
from karox.hosted_tools_runtime import (
    HostedToolsRuntime, BROWSER_OPEN, BROWSER_SNAPSHOT, BROWSER_CLOSE,
    BROWSER_CONSOLE, BROWSER_NETWORK, BROWSER_SCREENSHOT, BROWSER_CLICK,
    BROWSER_FILL, BROWSER_GET_TEXT, ARTIFACT_READ_IMAGE,
)
from karox.sessions import SessionStore
from karox.models import AccessProfile, Origin, OriginKind

REPO = Path(r"D:\проекты\faceboooook")
RTDIR = Path(tempfile.mkdtemp(prefix="kx-braud6-"))
for name in ("KAROX_VNEXT_RUNTIME_DIR", "KAROX_RUNTIME_DIR"):
    os.environ.pop(name, None)
os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(RTDIR / "runtime")
sessions = SessionStore(RTDIR / "sessions")
SID = "braud6"
sessions.create(repository=REPO, task="browser audit", access_profile=AccessProfile.WORKSPACE_WRITE, session_id=SID)
TOOLS = (BROWSER_OPEN, BROWSER_SNAPSHOT, BROWSER_CLOSE, BROWSER_CONSOLE, BROWSER_NETWORK,
         BROWSER_SCREENSHOT, BROWSER_CLICK, BROWSER_FILL, BROWSER_GET_TEXT, ARTIFACT_READ_IMAGE)
rt = HostedToolsRuntime(REPO, sessions, SID, TOOLS,
    access_profile=AccessProfile.WORKSPACE_WRITE,
    hosted_origin=Origin(OriginKind.HOSTED_CLIENT, SID))


def call(t, a, d=30):
    r = rt.execute(t, a, deadline_seconds=d)
    return r.structuredContent if hasattr(r, "structuredContent") else r


def nav(primary, view_id, sub=None):
    """Click a sidebar nav item (text match) and wait for the view to be active."""
    call(BROWSER_CLICK, {"selector": f'.sidebar .nav-item:has-text("{primary}")'}, d=15)
    if sub:
        try:
            call(BROWSER_CLICK, {"selector": f'#subNav button:has-text("{sub}")'}, d=10)
        except Exception:
            pass
    # wait for the view's .active class (Playwright CSS :has? no -- use JS state)
    # KaroX has no evaluate tool, so poll via wait_for on the active view.
    deadline = time.time() + 8
    active = False
    while time.time() < deadline:
        try:
            call(BROWSER_WAIT, {"selector": f"{view_id}.active", "state": "attached"}, d=3)
            active = True
            break
        except Exception:
            time.sleep(0.3)
    return active


from karox.hosted_tools_runtime import BROWSER_WAIT


def audit_screen(label):
    """Per-screen capture: snapshot + console + network + screenshot + read_image."""
    res = {}
    snap = call(BROWSER_SNAPSHOT, {}, d=30)
    inner = snap.get("snapshot", {}) if isinstance(snap, dict) else {}
    res["url"] = snap.get("url")
    res["title"] = snap.get("title")
    headings = inner.get("headings") or []
    res["headings"] = [h.get("text", "")[:60] for h in headings][:12]
    res["buttons_count"] = len(inner.get("buttons") or [])
    # capture a few distinguishing buttons (sample, not all)
    res["buttons_sample"] = [b.get("name", "")[:40] for b in (inner.get("buttons") or [])[:6]]
    res["inputs_count"] = len(inner.get("inputs") or [])
    res["issues_count"] = len(inner.get("issues") or [])
    res["dialogs"] = [d.get("name", "")[:40] for d in (inner.get("dialogs") or [])]
    # console
    con = call(BROWSER_CONSOLE, {}, d=15)
    entries = con.get("entries", con.get("messages", [])) if isinstance(con, dict) else []
    res["console_count"] = len(entries)
    res["console_sample"] = [str(e)[:80] for e in entries[:3]]
    # network_failures
    net = call(BROWSER_NETWORK, {}, d=15)
    res["network_failed_count"] = net.get("count", 0) if isinstance(net, dict) else 0
    res["network_failed"] = net.get("failed_requests", [])[:3] if isinstance(net, dict) else []
    # screenshot
    shot = call(BROWSER_SCREENSHOT, {"name": label, "full_page": True}, d=30)
    res["screenshot_ok"] = bool(shot.get("ok") or shot.get("artifact_id") or shot.get("image"))
    res["screenshot_artifact"] = shot.get("artifact_id") or shot.get("image_id")
    # artifact.read_image
    aid = shot.get("artifact_id") or shot.get("image_id")
    if aid:
        ri = call(ARTIFACT_READ_IMAGE, {"artifact_id": aid}, d=15)
        res["read_image_ok"] = bool(ri.get("ok") or ri.get("image") or ri.get("data"))
    else:
        res["read_image_ok"] = False
    return res


audit = {}

# Open localhost app
o = call(BROWSER_OPEN, {"url": "http://127.0.0.1:3000/"}, d=30)
print("=== OPEN === ok:", o.get("ok"), "url:", o.get("url"))

# SCREEN 1: access/workspace (the "Вход в рабочее пространство" gate, pre-login)
print("\n=== SCREEN: access_workspace ===")
audit["access_workspace"] = audit_screen("access_workspace")
print("buttons_sample:", audit["access_workspace"]["buttons_sample"])

# Enter workspace: local workspace login (NOT Facebook login).
call(BROWSER_FILL, {"selector": "#workspaceLoginInput", "value": "Smoke Owner"}, d=15)
call(BROWSER_FILL, {"selector": "#workspacePasswordInput", "value": "smoke-workspace-password"}, d=15)
call(BROWSER_CLICK, {"selector": "#workspaceAccessSubmit"}, d=15)
time.sleep(1.0)
# Dismiss onboarding wizard
try:
    call(BROWSER_WAIT, {"selector": "#onboardingModal.open", "state": "visible"}, d=5)
except Exception:
    pass
try:
    call(BROWSER_CLICK, {"selector": "#onboardingLaterBtn"}, d=10)
    time.sleep(0.8)
except Exception:
    pass

# SCREEN 2: dashboard (default landing after entry)
print("\n=== SCREEN: dashboard ===")
audit["dashboard"] = audit_screen("dashboard")
print("buttons_sample:", audit["dashboard"]["buttons_sample"])

# SCREEN 3: Группы (Groups) - view #groupsView
print("\n=== SCREEN: groups ===")
audit["groups"] = {"active": nav("Группы", "#groupsView"), **audit_screen("groups")}
print("active:", audit["groups"]["active"], "| buttons_sample:", audit["groups"]["buttons_sample"])

# SCREEN 4: Вакансии (Vacancies) - view #vacanciesView
print("\n=== SCREEN: vacancies ===")
audit["vacancies"] = {"active": nav("Вакансии", "#vacanciesView"), **audit_screen("vacancies")}
print("active:", audit["vacancies"]["active"], "| buttons_sample:", audit["vacancies"]["buttons_sample"])

# SCREEN 5: Постинг → План/Панель (Planner) - the planner is under Постинг; Автопостинг is #autopostView
# The user asked for "planner" -- the smoke test uses Постинг→Панель (#dashboardView) and Постинг→Автопостинг.
# We navigate Постинг→Автопостинг (#autopostView) as the planner/posting screen.
print("\n=== SCREEN: planner (Постинг) ===")
audit["planner"] = {"active": nav("Постинг", "#autopostView", sub="Автопостинг"), **audit_screen("planner")}
print("active:", audit["planner"]["active"], "| buttons_sample:", audit["planner"]["buttons_sample"])

# SCREEN 6: Аккаунты (Accounts) - view #accountsView
print("\n=== SCREEN: accounts ===")
audit["accounts"] = {"active": nav("Аккаунты", "#accountsView"), **audit_screen("accounts")}
print("active:", audit["accounts"]["active"], "| buttons_sample:", audit["accounts"]["buttons_sample"])

# SCREEN 7: Настройки (Settings) - view #settingsView
print("\n=== SCREEN: settings ===")
audit["settings"] = {"active": nav("Настройки", "#settingsView"), **audit_screen("settings")}
print("active:", audit["settings"]["active"], "| buttons_sample:", audit["settings"]["buttons_sample"])

# SCREEN 8: AI sidebar (toggle AI-ассистент)
print("\n=== SCREEN: ai_sidebar ===")
try:
    call(BROWSER_CLICK, {"selector": "text=AI-ассистент"}, d=15)
    time.sleep(0.8)
except Exception:
    pass
audit["ai_sidebar"] = audit_screen("ai_sidebar")
print("buttons_sample:", audit["ai_sidebar"]["buttons_sample"])

# SCREEN 9: light/dark theme (Настройки has a theme switch)
print("\n=== SCREEN: theme_dark ===")
audit["theme_dark"] = {"active": nav("Настройки", "#settingsView"), **audit_screen("theme_dark")}
# try a theme toggle in settings
for theme_sel in ["text=Тёмная тема", "text=Тёмная", "text=Dark theme", "button:has-text('тема')", "[data-theme]"]:
    try:
        call(BROWSER_CLICK, {"selector": theme_sel}, d=8)
        time.sleep(0.6)
        break
    except Exception:
        pass
audit["theme_dark"] = {**audit["theme_dark"], **audit_screen("theme_dark_after")}
print("buttons_sample:", audit["theme_dark"]["buttons_sample"])

# SCREEN 10: mobile layout (reopen at mobile viewport)
print("\n=== SCREEN: mobile_layout ===")
call(BROWSER_CLOSE, {}, d=15)
call(BROWSER_OPEN, {"url": "http://127.0.0.1:3000/", "width": 390, "height": 844}, d=30)
time.sleep(1.0)
call(BROWSER_FILL, {"selector": "#workspaceLoginInput", "value": "Smoke Owner"}, d=15)
call(BROWSER_FILL, {"selector": "#workspacePasswordInput", "value": "smoke-workspace-password"}, d=15)
call(BROWSER_CLICK, {"selector": "#workspaceAccessSubmit"}, d=15)
time.sleep(1.0)
try:
    call(BROWSER_CLICK, {"selector": "#onboardingLaterBtn"}, d=10)
    time.sleep(0.6)
except Exception:
    pass
audit["mobile_layout"] = audit_screen("mobile_layout")
print("viewport mobile captured | buttons_sample:", audit["mobile_layout"]["buttons_sample"])

# Close browser
close = call(BROWSER_CLOSE, {}, d=15)
print("\n=== CLOSE === ok:", close.get("ok"))

# Write full audit JSON
out = Path(r"D:\проекты\KaroX-v5\browser_audit_report.json")
out.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
print("\n=== AUDIT COMPLETE ===")
print("screens audited:", len(audit))
for k, v in audit.items():
    print(f"  {k}: active={v.get('active','n/a')} screenshot={v.get('screenshot_ok')} read_image={v.get('read_image_ok')} console={v.get('console_count')} net_fail={v.get('network_failed_count')}")
