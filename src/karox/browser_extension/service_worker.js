"use strict";

const MAX_NETWORK = 400;
const MAX_CONSOLE = 200;
const PING_MS = 20000;
const OVERLAY_HOST_SELECTOR = "[data-karox-overlay-host]";

const state = {
  socket: null,
  reconnectTimer: null,
  pingTimer: null,
  connected: false,
  takeover: false,
  agentTabId: null,
  agentGroupId: null,
  sessionId: null,
  lastSafeUrls: new Map(),
  requestStarted: new Map(),
  network: [],
  console: [],
  // B6 overlay delivery protocol:
  // desiredIndicator[tabId] = "controlling" | "takeover" | "resumed" | "hidden"
  // contentReady = Set of tabIds where content.js sent karox-content-ready
  // lastOverlayAck[tabId] = { ok: bool, at: number }
  // cursorPresent[tabId] = bool
  desiredIndicator: {},
  contentReady: new Set(),
  lastOverlayAck: {},
  cursorPresent: {},
};

function tabRef(tabId) {
  return "tab-" + tabId;
}

function parseTabRef(value) {
  if (typeof value !== "string" || !/^tab-\d+$/.test(value)) {
    throw new Error("invalid KaroX tab id");
  }
  return Number(value.slice(4));
}

function isWebUrl(url) {
  return typeof url === "string" && /^(https?:\/\/)/i.test(url);
}

function safeUrl(raw) {
  try {
    const value = new URL(raw);
    const parts = value.pathname.split("/").map((part) =>
      /^[A-Za-z0-9_+\/=.-]{24,}$/.test(part) ? "[REDACTED]" : part
    );
    const query = new URLSearchParams();
    for (const key of value.searchParams.keys()) {
      query.append(/token|secret|key|auth|session|code/i.test(key) ? "[REDACTED]" : key, "[REDACTED]");
    }
    return `${value.protocol}//${value.host}${parts.join("/")}${query.size ? `?${query}` : ""}`;
  } catch {
    return "";
  }
}

function trimNetwork() {
  while (state.network.length > MAX_NETWORK) state.network.shift();
}

function trimConsole() {
  while (state.console.length > MAX_CONSOLE) state.console.shift();
}

async function publishUiState() {
  const payload = {
    type: "karox-state",
    connected: state.connected,
    takeover: state.takeover,
    sessionId: state.sessionId,
    agentTabId: state.agentTabId ? tabRef(state.agentTabId) : null,
  };
  try {
    await chrome.runtime.sendMessage(payload);
  } catch {
    // The side panel is optional and may be closed.
  }
}

async function setBadge() {
  const text = state.takeover ? "YOU" : state.connected ? "AI" : "!";
  const color = state.takeover ? "#e3a12b" : state.connected ? "#35c98b" : "#a83f52";
  await chrome.action.setBadgeText({ text });
  await chrome.action.setBadgeBackgroundColor({ color });
  await chrome.action.setTitle({
    title: state.takeover
      ? "KaroX Browser · управление у пользователя"
      : state.connected
        ? "KaroX Browser · агент подключён"
        : "KaroX Browser · bridge не подключён",
  });
  await publishUiState();
}

async function loadConfig() {
  const response = await fetch(chrome.runtime.getURL("config.json"), { cache: "no-store" });
  if (!response.ok) throw new Error("KaroX extension config is unavailable");
  return response.json();
}

function scheduleReconnect() {
  if (state.reconnectTimer) return;
  state.reconnectTimer = setTimeout(() => {
    state.reconnectTimer = null;
    connectBridge().catch(() => scheduleReconnect());
  }, 1200);
}

async function connectBridge() {
  if (state.socket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(state.socket.readyState)) return;
  const config = await loadConfig();
  // B6: refuse to connect when config.json is a tombstone (legacy/personal-
  // contaminated dir). The bridge writes a disabled config there so a personal
  // Chrome extension never opens a socket to a new managed bridge.
  if (config.disabled || !config.websocket_url || !config.token) {
    scheduleReconnect();
    return;
  }
  state.sessionId = config.session_id || null;
  const socket = new WebSocket(`${config.websocket_url}?token=${encodeURIComponent(config.token)}`);
  state.socket = socket;

  socket.onopen = async () => {
    // B6: hello must echo the full managed-instance identity. The bridge
    // verifies every field and closes the socket on any mismatch, so a
    // personal Chrome (stale config) or a different saved profile is rejected.
    socket.send(JSON.stringify({
      type: "hello",
      extension_version: chrome.runtime.getManifest().version,
      session_id: config.session_id || null,
      bridge_instance_id: config.bridge_instance_id || null,
      browser_instance_id: config.browser_instance_id || null,
      saved_profile_id: config.saved_profile_id || null,
      launch_nonce: config.launch_nonce || null,
      instance_id: config.instance_id || null,
      browser: navigator.userAgent,
    }));
    clearInterval(state.pingTimer);
    state.pingTimer = setInterval(() => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "ping", at: Date.now() }));
      }
    }, PING_MS);
    await setBadge();
    // If an agent tab already exists, mark it as controlled again now that the
    // bridge is back. The in-page indicator survives navigation via manifest
    // re-injection, but on a fresh connect we re-assert state explicitly.
    if (state.agentTabId) {
      setIndicatorAck(state.agentTabId, state.takeover ? "takeover" : "controlling").catch(() => {});
    }
  };

  socket.onmessage = async (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (!message) return;
    // B6: the bridge acknowledges a verified hello. The extension only treats
    // itself as connected after this ack; if the bridge rejects the hello it
    // closes the socket and onclose clears state.connected.
    if (message.type === "hello_ack") {
      state.connected = true;
      await setBadge();
      return;
    }
    if (message.type !== "command" || typeof message.id !== "string") return;
    try {
      const result = await dispatchCommand(message.method, message.params || {});
      socket.send(JSON.stringify({ type: "result", id: message.id, ok: true, result }));
    } catch (error) {
      socket.send(JSON.stringify({
        type: "result",
        id: message.id,
        ok: false,
        error: String(error?.message || error).slice(0, 1000),
      }));
    }
  };

  socket.onclose = async () => {
    state.connected = false;
    clearInterval(state.pingTimer);
    state.pingTimer = null;
    await setBadge();
    // Bridge gone: drop every overlay so users never see a stale "KaroX manages
    // this tab" banner on a tab the agent can no longer reach.
    broadcastRemoveOverlay();
    scheduleReconnect();
  };

  socket.onerror = () => {
    try { socket.close(); } catch {}
  };
}

// Best-effort message to a content script. Never throws if the tab has no
// content script yet (chrome:// pages, not-yet-injected http pages, closed tab).
async function sendToTab(tabId, message) {
  try {
    await chrome.tabs.sendMessage(tabId, message);
  } catch {
    // content script may be absent; the action still proceeds without cursor.
  }
}

// Send a message to a tab and wait for an acknowledgement from content.js.
// Returns the ack response or throws if no ack / content.js absent.
// Retries up to 3 times with 200ms backoff (content.js may be mid-injection).
async function sendToTabAck(tabId, message, opts = {}) {
  const retries = opts.retries != null ? opts.retries : 3;
  const delayMs = opts.delayMs != null ? opts.delayMs : 200;
  let lastErr = null;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const ack = await new Promise((resolve, reject) => {
        chrome.tabs.sendMessage(tabId, message, function (response) {
          if (chrome.runtime.lastError) {
            reject(new Error(chrome.runtime.lastError.message));
          } else if (response && response.ok) {
            resolve(response);
          } else {
            reject(new Error("content script returned no ack or ok=false"));
          }
        });
      });
      state.lastOverlayAck[tabId] = { ok: true, at: Date.now() };
      return ack;
    } catch (err) {
      lastErr = err;
      if (attempt < retries) {
        await new Promise((r) => setTimeout(r, delayMs));
      }
    }
  }
  state.lastOverlayAck[tabId] = { ok: false, at: Date.now(), error: String(lastErr).slice(0, 200) };
  throw lastErr || new Error("sendToTabAck failed");
}

// Native Chrome tab-strip marker. The controlled tab lives in a one-tab group
// titled "KaroX", so the user can see agent ownership without any page overlay.
// Only the group created/remembered by this managed browser is cleaned up.
async function setAgentTabMarker(tabId, indicatorState) {
  const noGroup = chrome.tabGroups.TAB_GROUP_ID_NONE;
  if (state.agentGroupId != null && state.agentGroupId !== noGroup) {
    try {
      const grouped = await chrome.tabs.query({ groupId: state.agentGroupId });
      const otherIds = grouped.filter((tab) => tab.id !== tabId).map((tab) => tab.id);
      if (otherIds.length) await chrome.tabs.ungroup(otherIds);
    } catch {
      // Stale/deleted group; a fresh one is created below.
    }
  }
  const tab = await chrome.tabs.get(tabId);
  let groupId = tab.groupId;
  if (groupId === noGroup || groupId !== state.agentGroupId) {
    groupId = await chrome.tabs.group({ tabIds: [tabId] });
  }
  await chrome.tabGroups.update(groupId, {
    title: "KaroX",
    color: indicatorState === "takeover" ? "orange" : "blue",
    collapsed: false,
  });
  state.agentGroupId = groupId;
  return groupId;
}

// Store the agent/takeover state, update the native tab marker, and notify the
// content script so it can show/hide the agent cursor. The in-page status chip
// is intentionally invisible; ownership is represented by the native tab group.
async function setIndicatorAck(tabId, indicatorState) {
  state.desiredIndicator[tabId] = indicatorState;
  await setAgentTabMarker(tabId, indicatorState);
  try {
    await sendToTabAck(tabId, { type: "karox-set-indicator", state: indicatorState });
  } catch (err) {
    // Stored desired state will be re-applied when content.js sends ready.
    throw err;
  }
}

async function broadcastRemoveOverlay() {
  try {
    const tabs = await allTabs();
    for (const tab of tabs) {
      if (isWebUrl(tab.url || "")) {
        await sendToTab(tab.id, { type: "karox-remove-overlay" });
      }
    }
  } catch {
    // best-effort
  }
}

async function allTabs() {
  return chrome.tabs.query({});
}

async function getTab(tabId) {
  try {
    return await chrome.tabs.get(tabId);
  } catch {
    return null;
  }
}

async function ensureAgentTab() {
  if (state.agentTabId) {
    const existing = await getTab(state.agentTabId);
    if (existing) {
      const url = existing.url || "";
      // Trust agentTabId when we set it explicitly (open/new_tab/switch_tab),
      // even during the navigation transition when the tab URL may temporarily
      // read as about:blank or a pending commit. lastSafeUrls records the
      // intended URL, so a race with a background navigation never switches
      // control to an unrelated pre-existing tab (e.g. a stale ChatGPT tab).
      if (isWebUrl(url) || state.lastSafeUrls.has(state.agentTabId)) return existing;
    }
  }
  const tabs = await allTabs();
  // Reuse Chrome's single startup tab (usually about:blank) before creating
  // anything new. This prevents a stranded blank tab when the first MCP open
  // response is lost between browser launch and navigation.
  const candidate = tabs.find((tab) => isWebUrl(tab.url || "")) || tabs[0];
  if (candidate) {
    state.agentTabId = candidate.id;
    return candidate;
  }
  const created = await chrome.tabs.create({ url: "about:blank", active: false });
  state.agentTabId = created.id;
  return created;
}

function snapshotPage() {
  const secretHint = /password|secret|token|api[_-]?key|credential|cookie|card|cvv|cvc/i;
  const out = { headings: [], buttons: [], inputs: [], links: [], dialogs: [], tabs: [], text: "" };
  const collected = [];
  const nodes = Array.from(document.querySelectorAll(
    "h1,h2,h3,h4,h5,h6,button,input,select,textarea,a,[role=button],[role=dialog],[role=tab]"
  ))
    // Defence-in-depth: never include KaroX's own overlay host in snapshots or
    // text. The overlay lives in a closed shadow DOM, so it is already invisible
    // to querySelectorAll; this filter is a second boundary for safety.
    .filter((el) => !el.closest(OVERLAY_HOST_SELECTOR))
    .slice(0, 700);
  for (const el of nodes) {
    const tag = el.tagName.toLowerCase();
    const label = (el.getAttribute("aria-label") || el.innerText || el.placeholder || "").trim().slice(0, 240);
    if (/^h[1-6]$/.test(tag)) out.headings.push({ level: tag, text: label });
    else if (tag === "button" || el.getAttribute("role") === "button") {
      out.buttons.push({ name: label, disabled: Boolean(el.disabled) });
    } else if (["input", "select", "textarea"].includes(tag)) {
      const type = (el.getAttribute("type") || tag).toLowerCase();
      const secret = el.getAttribute("data-karox-secret") === "true" || el.dataset.karoxSecret === "true" || type === "password" || secretHint.test([el.name, el.id, label].join(" "));
      out.inputs.push({
        label,
        type,
        disabled: Boolean(el.disabled),
        secret,
        value_length: secret ? null : String(el.value || "").length,
      });
    } else if (tag === "a") {
      out.links.push({ text: label, href: String(el.getAttribute("href") || "").slice(0, 400) });
    } else if (el.getAttribute("role") === "dialog") out.dialogs.push({ name: label });
    else if (el.getAttribute("role") === "tab") {
      out.tabs.push({ text: label, selected: el.getAttribute("aria-selected") === "true" });
    }
    if (label) collected.push(label);
  }
  out.text = collected.join(" ").slice(0, 30000);
  out.scroll = {
    width: document.documentElement.scrollWidth,
    height: document.documentElement.scrollHeight,
    viewport_width: window.innerWidth,
    viewport_height: window.innerHeight,
    horizontal_overflow: document.documentElement.scrollWidth > window.innerWidth + 1,
  };
  return {
    title: document.title,
    url: location.href,
    focused_element: document.activeElement?.tagName?.toLowerCase() || null,
    viewport: { width: window.innerWidth, height: window.innerHeight },
    snapshot: out,
  };
}

function resolveSelector(selector) {
  const labelTarget = (text) => {
    const wanted = text.trim().toLowerCase();
    for (const label of document.querySelectorAll("label")) {
      const labelText = (label.innerText || label.textContent || "").trim().toLowerCase();
      if (labelText === wanted || labelText.includes(wanted)) {
        if (label.htmlFor) {
          const byId = document.getElementById(label.htmlFor);
          if (byId) return byId;
        }
        const nested = label.querySelector("input,textarea,select,button");
        if (nested) return nested;
      }
    }
    return null;
  };
  const textTarget = (text) => {
    const wanted = text.trim().toLowerCase();
    const candidates = document.querySelectorAll("button,a,input[type=submit],[role=button],[role=link],label");
    for (const el of candidates) {
      const value = (el.innerText || el.value || el.getAttribute("aria-label") || "").trim().toLowerCase();
      if ((value === wanted || value.includes(wanted)) && visible(el)) return el;
    }
    return null;
  };
  const roleTarget = (raw) => {
    const match = raw.match(/^([^\[]+)(?:\[name=["']?(.*?)["']?\])?$/i);
    if (!match) return null;
    const role = match[1].trim();
    const name = (match[2] || "").trim().toLowerCase();
    for (const el of document.querySelectorAll(`[role="${CSS.escape(role)}"],${role === "button" ? "button" : "__none__"}`)) {
      const value = (el.innerText || el.value || el.getAttribute("aria-label") || "").trim().toLowerCase();
      if ((!name || value === name || value.includes(name)) && visible(el)) return el;
    }
    return null;
  };
  if (selector.startsWith("text=")) return textTarget(selector.slice(5));
  if (selector.startsWith("label=")) return labelTarget(selector.slice(6));
  if (selector.startsWith("role=")) return roleTarget(selector.slice(5));
  try { return document.querySelector(selector); } catch { return null; }
}

function visible(el) {
  if (!el) return false;
  const style = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity || 1) > 0 && rect.width >= 0 && rect.height >= 0;
}

// domAction moved to dom_helpers.js so the deterministic fixture suite tests
// the exact engine production injects. executeScript cannot serialize a
// closure over the worker's globals, so this proxy resolves the global that
// the files-inject of dom_helpers.js provides in the ISOLATED world.
function domActionProxy(payload) {
  return domAction(payload);
}

async function executeScript(tabId, func, args = []) {
  // Inject dom_helpers.js (resolveSelector, visible, OVERLAY_HOST_SELECTOR)
  // into the ISOLATED world before running func. chrome.scripting.executeScript
  // does NOT transfer the service worker's closure, so func cannot reference
  // service_worker globals (OVERLAY_HOST_SELECTOR) or other named functions
  // (resolveSelector, visible). The files-inject makes them available as
  // globals in the ISOLATED world. Idempotent: re-injecting with `var` is safe.
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      world: "ISOLATED",
      files: ["dom_helpers.js"],
    });
  } catch (e) {
    // Ignore: file may already be injected or page not ready; func will surface
    // its own error if a helper is missing.
  }
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: "ISOLATED",
    func,
    args,
  });
  if (!results.length) throw new Error("page returned no script result");
  return results[0].result;
}

async function dom(tabId, action, params = {}) {
  return executeScript(tabId, domActionProxy, [{ action, ...params }]);
}

// Resolve the centre of the target element so the cursor can travel to it
// before the DOM action fires. Returns null if the element is missing or has
// no box (display:none, detached); callers then skip the cursor and let the
// action itself surface the element-not-found error.
async function targetCenter(tabId, selector) {
  return executeScript(
    tabId,
    (sel, hostSel) => {
      function resolve(s) {
        const labelTarget = (text) => {
          const wanted = text.trim().toLowerCase();
          for (const label of document.querySelectorAll("label")) {
            const labelText = (label.innerText || label.textContent || "").trim().toLowerCase();
            if (labelText === wanted || labelText.includes(wanted)) {
              if (label.htmlFor) {
                const byId = document.getElementById(label.htmlFor);
                if (byId) return byId;
              }
              const nested = label.querySelector("input,textarea,select,button");
              if (nested) return nested;
            }
          }
          return null;
        };
        const textTarget = (text) => {
          const wanted = text.trim().toLowerCase();
          const candidates = document.querySelectorAll("button,a,input[type=submit],[role=button],[role=link],label");
          for (const el of candidates) {
            const value = (el.innerText || el.value || el.getAttribute("aria-label") || "").trim().toLowerCase();
            if ((value === wanted || value.includes(wanted)) && visible(el)) return el;
          }
          return null;
        };
        const roleTarget = (raw) => {
          const match = raw.match(/^([^\[]+)(?:\[name=["']?(.*?)["']?\])?$/i);
          if (!match) return null;
          const role = match[1].trim();
          const name = (match[2] || "").trim().toLowerCase();
          for (const el of document.querySelectorAll(`[role="${CSS.escape(role)}"],${role === "button" ? "button" : "__none__"}`)) {
            const value = (el.innerText || el.value || el.getAttribute("aria-label") || "").trim().toLowerCase();
            if ((!name || value === name || value.includes(name)) && visible(el)) return el;
          }
          return null;
        };
        function visible(el) {
          if (!el) return false;
          const style = getComputedStyle(el);
          const rect = el.getBoundingClientRect();
          return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity || 1) > 0 && rect.width >= 0 && rect.height >= 0;
        }
        if (s.startsWith("text=")) return textTarget(s.slice(5));
        if (s.startsWith("label=")) return labelTarget(s.slice(6));
        if (s.startsWith("role=")) return roleTarget(s.slice(5));
        try { return document.querySelector(s); } catch { return null; }
      }
      const el = resolve(String(sel || ""));
      if (!el) return null;
      if (el.closest && el.closest(hostSel)) return null;
      const r = el.getBoundingClientRect();
      if (!r || (r.width === 0 && r.height === 0)) return null;
      return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2), w: Math.round(r.width), h: Math.round(r.height) };
    },
    [String(selector || ""), OVERLAY_HOST_SELECTOR],
  );
}

// Move the visible cursor to the target before a DOM input action. Never blocks
// the action on cursor failure; if the content script is absent the action
// still runs (no cursor, but correct behaviour). Re-checked against takeover
// before and after by the caller.
async function moveCursorTo(tabId, selector, actionLabel) {
  if (state.takeover) return; // defensive; caller already throws
  let center = null;
  try {
    center = await targetCenter(tabId, selector);
  } catch {
    return; // page not ready; let the action surface its own error
  }
  if (!center || typeof center.x !== "number") return;
  try {
    await sendToTabAck(tabId, {
      type: "karox-move-cursor",
      x: center.x,
      y: center.y,
      action: actionLabel,
    });
    state.cursorPresent[tabId] = true;
  } catch {
    // content.js absent; the action still proceeds without cursor.
  }
}

async function currentAgentTab() {
  const tab = await ensureAgentTab();
  if (!tab?.id) throw new Error("no KaroX work tab is available");
  return tab;
}

// Query the actually-visible active tab in the focused window.
// This is the ground truth for what the user sees, not state.agentTabId.
async function visibleActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return tabs.length ? tabs[0] : null;
}

// Activate a tab and verify it became the visible active tab in its window.
// Returns the activated tab or throws if activation failed.
async function activateAndVerifyTab(tabId) {
  const before = await chrome.tabs.get(tabId);
  await chrome.tabs.update(tabId, { active: true });
  // The managed browser normally starts minimized so background work does not
  // steal focus. User takeover must restore the owned window before verifying
  // that this tab is actually visible.
  await chrome.windows.update(before.windowId, { state: "normal", focused: true });
  // Re-query to verify activation succeeded.
  const after = await chrome.tabs.get(tabId);
  if (!after.active) {
    throw new Error(`tab ${tabRef(tabId)} did not become active after chrome.tabs.update`);
  }
  // Verify it's the visible active tab in its window.
  const visibleTabs = await chrome.tabs.query({ active: true, windowId: before.windowId });
  if (!visibleTabs.length || visibleTabs[0].id !== tabId) {
    throw new Error(`tab ${tabRef(tabId)} is not the visible active tab in window ${before.windowId}`);
  }
  return after;
}

async function listPublicTabs() {
  const tabs = await allTabs();
  return tabs.map((tab) => ({
    tab_id: tabRef(tab.id),
    active: tab.id === state.agentTabId,
    browser_active: Boolean(tab.active),
    url: safeUrl(tab.url || tab.pendingUrl || ""),
    title: String(tab.title || "").slice(0, 300),
    window_id: tab.windowId,
  }));
}

function assertAgentInputAllowed(method) {
  if (state.takeover) {
    throw new Error("user takeover is active; agent " + method + " is paused until resume_after_user_takeover");
  }
}

async function dispatchCommand(method, params) {
  if (method === "status") {
    return { connected: state.connected, takeover: state.takeover, session_id: state.sessionId };
  }
  if (method === "open") {
    assertAgentInputAllowed("open");
    let tab = await currentAgentTab();
    tab = await chrome.tabs.update(tab.id, { url: String(params.url), active: false });
    state.agentTabId = tab.id;
    if (isWebUrl(params.url)) state.lastSafeUrls.set(tab.id, String(params.url));
    const desired = state.takeover ? "takeover" : "controlling";
    state.desiredIndicator[tab.id] = desired;
    // The native tab marker does not depend on page/content-script readiness,
    // so show ownership in the Chrome strip immediately after navigation starts.
    await setAgentTabMarker(tab.id, desired);
    // Content-script state is best-effort and only drives the agent cursor now.
    setTimeout(() => {
      sendToTab(tab.id, {
        type: "karox-set-indicator",
        state: desired,
      });
    }, 350);
    return { open: true, tab_id: tabRef(tab.id), url: safeUrl(params.url), background: true };
  }
  if (method === "tabs") {
    const tabs = await listPublicTabs();
    return {
      tabs,
      count: tabs.length,
      active_tab_id: state.agentTabId ? tabRef(state.agentTabId) : null,
      takeover_active: state.takeover,
    };
  }
  if (method === "new_tab") {
    assertAgentInputAllowed("new_tab");
    const tab = await chrome.tabs.create({ url: params.url || "about:blank", active: true });
    state.agentTabId = tab.id;
    if (isWebUrl(params.url || "")) state.lastSafeUrls.set(tab.id, String(params.url));
    const desired = state.takeover ? "takeover" : "controlling";
    state.desiredIndicator[tab.id] = desired;
    await setAgentTabMarker(tab.id, desired);
    return { created: true, tab_id: tabRef(tab.id), url: safeUrl(tab.url || params.url || ""), background: false };
  }
  if (method === "switch_tab") {
    assertAgentInputAllowed("switch_tab");
    const tabId = parseTabRef(params.tab_id);
    const tab = await getTab(tabId);
    if (!tab) throw new Error("tab does not exist");
    // Clear the indicator on the previously controlled tab, then set it on the
    // new one so only one tab at a time shows the KaroX banner.
    if (state.agentTabId && state.agentTabId !== tabId) {
      await sendToTab(state.agentTabId, { type: "karox-remove-overlay" });
    }
    state.agentTabId = tabId;
    await setIndicatorAck(tabId, state.takeover ? "takeover" : "controlling");
    return { switched: true, tab_id: tabRef(tabId), url: safeUrl(tab.url || ""), background: true };
  }
  if (method === "close_tab") {
    assertAgentInputAllowed("close_tab");
    const tabId = parseTabRef(params.tab_id);
    // Best-effort: tell the doomed tab to drop its overlay before it goes.
    await sendToTab(tabId, { type: "karox-remove-overlay" });
    const workTabs = (await allTabs()).filter((tab) => tab.id !== tabId);
    if (workTabs.length < 1) throw new Error("cannot close the last remaining tab");
    await chrome.tabs.remove(tabId);
    state.lastSafeUrls.delete(tabId);
    delete state.desiredIndicator[tabId];
    delete state.lastOverlayAck[tabId];
    delete state.cursorPresent[tabId];
    state.contentReady.delete(tabId);
    if (state.agentTabId === tabId) {
      const next = workTabs.find((tab) => tab.id !== tabId);
      state.agentTabId = next?.id || null;
      if (next?.id) {
        const desired = state.takeover ? "takeover" : "controlling";
        state.desiredIndicator[next.id] = desired;
        await setAgentTabMarker(next.id, desired);
      } else {
        state.agentGroupId = null;
      }
    }
    return { closed: true, tab_id: tabRef(tabId), active_tab_id: state.agentTabId ? tabRef(state.agentTabId) : null };
  }
  if (method === "snapshot") {
    const tab = await currentAgentTab();
    const result = await executeScript(tab.id, snapshotPage);
    return { ...result, tab_id: tabRef(tab.id) };
  }
  if (method === "inspect") {
    const tab = await currentAgentTab();
    return dom(tab.id, "inspect", { selector: params.selector });
  }
  if (method === "debug_dom") {
    // Diagnostic: verify executeScript can read the page DOM.
    const tab = await currentAgentTab();
    const result = await executeScript(tab.id, () => ({
      title: document.title,
      url: location.href,
      ready: document.readyState,
      body_present: !!document.body,
      body_len: document.body ? document.body.innerHTML.length : -1,
      h1_count: document.querySelectorAll("h1").length,
      h1_text: (document.querySelector("h1") || {}).innerText || null,
      all_count: document.querySelectorAll("*").length,
    }));
    return { ...result, tab_id: tabRef(tab.id), tab_url: safeUrl(tab.url || "") };
  }
  if ([
    "click", "dblclick", "hover", "focus", "clear", "fill", "fill_secret",
    "type", "select", "press", "set_checked", "scroll", "upload", "get_text",
  ].includes(method)) {
    // Double-checked takeover: before move, and again before the DOM action,
    // so a takeover that arrives between move and click is honoured and never
    // lets a queued action fire on the user's tab.
    assertAgentInputAllowed(method);
    const tab = await currentAgentTab();
    const action = method === "get_text" ? "text" : method;
    const readOnly = method === "get_text";
    const pageScoped = method === "scroll" && !params.selector;
    if (!readOnly && !pageScoped) {
      await moveCursorTo(tab.id, params.selector, action);
      assertAgentInputAllowed(method); // re-check after cursor move
    }
    return dom(tab.id, action, params);
  }
  if (method === "wait_for") {
    const tab = await currentAgentTab();
    if (params.selector) {
      const deadline = Date.now() + Math.max(1000, Math.min(Number(params.timeout_ms || 15000), 60000));
      const wanted = params.state || "visible";
      while (Date.now() < deadline) {
        try {
          const meta = await dom(tab.id, "inspect", { selector: params.selector });
          const matches = wanted === "attached" || (wanted === "visible" && meta.visible) || (wanted === "hidden" && !meta.visible);
          if (matches) return { waited: true, selector: params.selector, state: wanted };
        } catch (error) {
          if (wanted === "detached" || wanted === "hidden") return { waited: true, selector: params.selector, state: wanted };
        }
        await new Promise((resolve) => setTimeout(resolve, 120));
      }
      throw new Error("wait_for timed out");
    }
    const milliseconds = Math.max(1, Math.min(Number(params.milliseconds || 250), 10000));
    await new Promise((resolve) => setTimeout(resolve, milliseconds));
    return { waited: true, milliseconds };
  }
  if (method === "screenshot") {
    const tab = await currentAgentTab();
    // Element screenshots crop the viewport capture to the target's box.
    // The element is scrolled into view first -- unless the user has taken
    // over, in which case the agent must not move their viewport.
    let clip = null;
    if (params.selector) {
      if (!state.takeover) {
        await dom(tab.id, "scroll", { selector: String(params.selector), mode: "into_view" });
      }
      const meta = await dom(tab.id, "inspect", { selector: String(params.selector) });
      if (meta && meta.error_kind) return meta;
      const dpr = await executeScript(tab.id, () => window.devicePixelRatio || 1);
      if (!meta || !meta.rect || meta.rect.width <= 0 || meta.rect.height <= 0) {
        throw new Error("element_not_found: the element has no visible box to screenshot");
      }
      const scale = Number(dpr) || 1;
      clip = {
        x: Math.max(0, Math.round(meta.rect.x * scale)),
        y: Math.max(0, Math.round(meta.rect.y * scale)),
        width: Math.max(1, Math.round(meta.rect.width * scale)),
        height: Math.max(1, Math.round(meta.rect.height * scale)),
      };
    }
    const previous = (await chrome.tabs.query({ active: true, windowId: tab.windowId }))[0];
    await chrome.tabs.update(tab.id, { active: true });
    let data_url = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
    if (previous?.id && previous.id !== tab.id) await chrome.tabs.update(previous.id, { active: true });
    if (clip) {
      const blob = await (await fetch(data_url)).blob();
      const bitmap = await createImageBitmap(blob);
      const x = Math.min(clip.x, Math.max(0, bitmap.width - 1));
      const y = Math.min(clip.y, Math.max(0, bitmap.height - 1));
      const width = Math.max(1, Math.min(clip.width, bitmap.width - x));
      const height = Math.max(1, Math.min(clip.height, bitmap.height - y));
      const canvas = new OffscreenCanvas(width, height);
      canvas.getContext("2d").drawImage(bitmap, x, y, width, height, 0, 0, width, height);
      const cropped = await canvas.convertToBlob({ type: "image/png" });
      const buffer = new Uint8Array(await cropped.arrayBuffer());
      let binary = "";
      const chunk = 0x8000;
      for (let offset = 0; offset < buffer.length; offset += chunk) {
        binary += String.fromCharCode.apply(null, buffer.subarray(offset, offset + chunk));
      }
      data_url = "data:image/png;base64," + btoa(binary);
      return { data_url, tab_id: tabRef(tab.id), full_page: false, viewport_only: false, element: true };
    }
    return { data_url, tab_id: tabRef(tab.id), full_page: false, viewport_only: true };
  }
  if (method === "network") {
    return { requests: state.network.slice(), count: state.network.length };
  }
  if (method === "console") {
    // Real page-console capture now flows MAIN -> ISOLATED -> here. Logs that
    // occurred before the content script injected, or during a navigation
    // before re-injection, are missed; capture is best-effort and never blocks
    // browser commands.
    return {
      entries: state.console.slice(),
      count: state.console.length,
      supported: true,
      note: "best-effort MAIN-world relay; logs before injection or during navigation may be missed",
    };
  }
  if (method === "debug_overlay_state") {
    // Runtime diagnostics: query the live content-script state in the agent tab.
    const tab = await currentAgentTab();
    let live = null;
    try {
      live = await sendToTabAck(tab.id, { type: "karox-get-state" }, { retries: 1, delayMs: 100 });
    } catch (err) {
      live = { ok: false, error: String(err).slice(0, 200) };
    }
    // Query the actually-visible active tab (ground truth for what user sees).
    const visible = await visibleActiveTab();
    const visibleTabId = visible ? visible.id : null;
    const visibleWindowId = visible ? visible.windowId : null;
    const allEqual = visibleTabId === tab.id && (live?.overlay_present || false);
    return {
      target_tab_id: tabRef(tab.id),
      visible_active_tab_id: visibleTabId ? tabRef(visibleTabId) : null,
      visible_window_id: visibleWindowId,
      content_script_ready: state.contentReady.has(tab.id),
      desired_indicator: state.desiredIndicator[tab.id] || null,
      last_overlay_ack: state.lastOverlayAck[tab.id] || null,
      cursor_present: state.cursorPresent[tab.id] || false,
      live_overlay_present: live?.overlay_present || false,
      live_indicator_state: live?.indicator_state || "unknown",
      live_cursor_present: live?.cursor_present || false,
      live_indicator_text: live?.indicator_text || null,
      live_document_url: live?.document_url || null,
      takeover_active: state.takeover,
      ownership_target_tab_id: tabRef(tab.id),
      overlay_ack_tab_id: state.lastOverlayAck[tab.id] ? tabRef(tab.id) : null,
      all_equal: allEqual,
    };
  }
  if (method === "geometry") {
    // Detailed rendered geometry for visual verification.
    const tab = await currentAgentTab();
    let geo = null;
    try {
      geo = await sendToTabAck(tab.id, { type: "karox-get-geometry" }, 5000);
    } catch (err) {
      geo = { ok: false, error: String((err && err.message) || err).slice(0, 300) };
    }
    return { tab_id: tabRef(tab.id), geometry: geo };
  }
  if (method === "back" || method === "forward") {
    // History navigation on the agent tab. Same takeover contract as open:
    // the agent never navigates a tab the user has taken over.
    assertAgentInputAllowed(method);
    const tab = await currentAgentTab();
    if (method === "back") await chrome.tabs.goBack(tab.id);
    else await chrome.tabs.goForward(tab.id);
    // Navigation commits asynchronously; poll briefly for the settled URL so
    // the evidence names where the tab actually ended up.
    let settled = tab;
    for (let i = 0; i < 20; i++) {
      await new Promise((resolve) => setTimeout(resolve, 150));
      settled = await getTab(tab.id);
      if (settled && settled.status === "complete") break;
    }
    return {
      action: method,
      result: "success",
      navigated: true,
      tab_id: tabRef(tab.id),
      tab_url: safeUrl((settled && settled.url) || ""),
      title: String((settled && settled.title) || "").slice(0, 300),
    };
  }
  if (method === "page_info") {
    // Cheap read of where the agent tab is, without a full snapshot.
    const tab = await currentAgentTab();
    return {
      tab_id: tabRef(tab.id),
      tab_url: safeUrl(tab.url || ""),
      title: String(tab.title || "").slice(0, 300),
      status: String(tab.status || ""),
    };
  }
  if (method === "download") {
    // Explicit download through the browser's own download manager. Requires
    // the optional "downloads" permission; refusal is a typed permission
    // error, never a silent generic failure.
    assertAgentInputAllowed("download");
    if (!chrome.downloads || !chrome.downloads.download) {
      throw new Error("permission_denied: the downloads permission is not granted to the KaroX extension");
    }
    const url = String(params.url || "");
    if (!/^https?:\/\//i.test(url)) throw new Error("download url must be http(s)");
    const downloadId = await chrome.downloads.download({ url, saveAs: false });
    const deadline = Date.now() + Math.max(1000, Math.min(Number(params.timeout_ms || 60000), 300000));
    let last = null;
    while (Date.now() < deadline) {
      const found = await chrome.downloads.search({ id: downloadId });
      last = found && found[0];
      if (last && last.state === "complete") {
        return {
          action: "download",
          result: "success",
          download_id: downloadId,
          filename: String(last.filename || "").split(/[\\/]/).pop(),
          bytes: Number(last.totalBytes || last.bytesReceived || 0),
          state: "complete",
        };
      }
      if (last && last.state === "interrupted") {
        throw new Error("download_failure: " + String(last.error || "interrupted"));
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    try { await chrome.downloads.cancel(downloadId); } catch (e) {}
    throw new Error("download_failure: timed out waiting for completion");
  }
  if (method === "reload_page") {
    // Reload the current agent tab to re-inject the content script.
    const tab = await currentAgentTab();
    await chrome.tabs.update(tab.id, { active: true });
    await chrome.tabs.reload(tab.id);
    // Wait for content script ready (up to 10s).
    for (let i = 0; i < 20; i++) {
      if (state.contentReady.has(tab.id)) break;
      await new Promise((r) => setTimeout(r, 500));
    }
    // Re-apply indicator + cursor after reload.
    state.desiredIndicator[tab.id] = state.takeover ? "takeover" : "controlling";
    try { await setIndicatorAck(tab.id, state.desiredIndicator[tab.id]); } catch {}
    try {
      await sendToTabAck(tab.id, { type: "karox-move-cursor", x: 720, y: 450, action: "" });
      state.cursorPresent[tab.id] = true;
    } catch {}
    return { reloaded: true, tab_id: tabRef(tab.id), content_ready: state.contentReady.has(tab.id) };
  }
  if (method === "takeover") {
    const tab = await currentAgentTab();
    // B6 visible-active-tab contract: takeover is ONLY valid for the tab the
    // user actually sees. Activate the target tab, verify it became the visible
    // active tab, and only then apply the indicator. If activation fails, fail
    // safely — do NOT pause agent input for a tab the user cannot see.
    let activatedTab;
    try {
      activatedTab = await activateAndVerifyTab(tab.id);
    } catch (err) {
      return {
        takeover: false,
        agent_input_paused: false,
        overlay_ack: false,
        visual_takeover_failed: true,
        error: String(err).slice(0, 300),
        tab_id: tabRef(tab.id),
        url: safeUrl(tab.url || ""),
      };
    }
    state.takeover = true;
    state.agentTabId = tab.id;
    state.desiredIndicator[tab.id] = "takeover";
    await setBadge();
    // In-page indicator switches to "Управление передано вами" and the cursor
    // is hidden until the agent resumes.  Ack required: takeover is NOT
    // success until the content script confirms the overlay was rendered.
    let overlayAck = null;
    try {
      overlayAck = await setIndicatorAck(tab.id, "takeover");
    } catch (err) {
      // Overlay delivery failed. Still pause input (defensive) but report failure.
      return {
        takeover: true,
        agent_input_paused: true,
        overlay_ack: false,
        overlay_error: String(err).slice(0, 200),
        visual_active_tab_id: tabRef(activatedTab.id),
        visible_window_id: activatedTab.windowId,
        tab_id: tabRef(tab.id),
        url: safeUrl(tab.url || ""),
      };
    }
    await sendToTabAck(tab.id, { type: "karox-hide-cursor" }).catch(() => {});
    // Verify the visible active tab still matches after overlay applied.
    const visibleNow = await visibleActiveTab();
    const allEqual = visibleNow && visibleNow.id === tab.id;
    return {
      takeover: true,
      agent_input_paused: true,
      overlay_ack: true,
      visual_active_tab_id: tabRef(activatedTab.id),
      visible_window_id: activatedTab.windowId,
      overlay_ack_tab_id: tabRef(tab.id),
      visible_active_matches_overlay: allEqual,
      tab_id: tabRef(tab.id),
      url: safeUrl(tab.url || ""),
    };
  }
  if (method === "resume") {
    state.takeover = false;
    await setBadge();
    const tab = await currentAgentTab();
    state.desiredIndicator[tab.id] = "resumed";
    // Transient "KaroX продолжил работу" then auto-revert to "KaroX управляет
    // этой вкладкой" inside the content script.  Ack required.
    try {
      await setIndicatorAck(tab.id, "resumed");
    } catch (err) {
      return {
        resumed: true,
        agent_input_paused: false,
        overlay_ack: false,
        overlay_error: String(err).slice(0, 200),
        tab_id: tabRef(tab.id),
        url: safeUrl(tab.url || ""),
      };
    }
    return { resumed: true, agent_input_paused: false, overlay_ack: true, tab_id: tabRef(tab.id), url: safeUrl(tab.url || "") };
  }
  if (method === "recover") {
    const tab = await currentAgentTab();
    if ((tab.url || "") === "about:blank" && state.lastSafeUrls.has(tab.id)) {
      const target = state.lastSafeUrls.get(tab.id);
      await chrome.tabs.update(tab.id, { url: target, active: false });
      return { recovered: true, tab_id: tabRef(tab.id), url: safeUrl(target) };
    }
    return { recovered: false, tab_id: tabRef(tab.id), url: safeUrl(tab.url || "") };
  }
  if (method === "open_window") {
    // B6: create a single window with the startup URL. Called after --no-startup-window
    // launch so Chrome never creates an extra about:blank tab.
    const startupUrl = String(params.url || "");
    if (!startupUrl) throw new Error("open_window requires a url");
    const win = await chrome.windows.create({ url: startupUrl, type: "normal" });
    const tab = win.tabs[0];
    state.agentTabId = tab.id;
    if (isWebUrl(startupUrl)) state.lastSafeUrls.set(tab.id, startupUrl);
    state.desiredIndicator[tab.id] = "controlling";
    // Wait for content script ready (up to 15s).
    for (let i = 0; i < 30; i++) {
      if (state.contentReady.has(tab.id)) break;
      await new Promise((r) => setTimeout(r, 500));
    }
    // Apply the controlling indicator + cursor.
    try { await setIndicatorAck(tab.id, "controlling"); } catch {}
    try {
      await sendToTabAck(tab.id, { type: "karox-move-cursor", x: 720, y: 450, action: "" });
      state.cursorPresent[tab.id] = true;
    } catch {}
    return {
      opened: true,
      tab_id: tabRef(tab.id),
      window_id: win.id,
      url: safeUrl(tab.url || startupUrl),
      content_ready: state.contentReady.has(tab.id),
    };
  }
  if (method === "show_window") {
    // B6: move the window to visible area after offscreen launch.
    // Called after navigate_to completes so the user only sees the final
    // single-tab state, never about:blank creation/navigate.
    const left = Number(params.left) || 100;
    const top = Number(params.top) || 100;
    const tab = await currentAgentTab();
    await chrome.windows.update(tab.windowId, { state: "normal", left: left, top: top, focused: true });
    return { shown: true, window_id: tab.windowId, left: left, top: top };
  }
  if (method === "tabs_all") {
    // B6: ground-truth tab count — ALL visible tabs in ALL windows, not just
    // owned/registry tabs. Used for startup acceptance.
    const allTabs = await chrome.tabs.query({});
    const windows = await chrome.windows.getAll();
    const result = {
      browser_instance_id: state.browserInstanceId || null,
      window_count: windows.length,
      browser_visible_tabs_count: allTabs.length,
      registry_owned_tabs_count: 0,
      unowned_tabs_count: 0,
      tabs: [],
    };
    for (const tab of allTabs) {
      const tid = tabRef(tab.id);
      const isOwned = state.agentTabId === tab.id;
      if (isOwned) result.registry_owned_tabs_count++;
      else result.unowned_tabs_count++;
      result.tabs.push({
        tab_id: tid,
        window_id: tab.windowId,
        url: safeUrl(tab.url || tab.pendingUrl || ""),
        title: String(tab.title || "").slice(0, 200),
        active: Boolean(tab.active),
        highlighted: Boolean(tab.highlighted),
        status: tab.status || "unknown",
        ownership: isOwned ? "owned" : "unowned",
        created_by_karox: isOwned,
        is_initial: !isOwned,
      });
    }
    return result;
  }
  if (method === "navigate_to") {
    // B6: navigate the existing single tab to the startup URL. Called after
    // Chrome launches with about:blank so we never create or close tabs.
    // Lifecycle: 1 tab created, 1 tab navigated, 0 tabs closed.
    const startupUrl = String(params.url || "");
    if (!startupUrl) throw new Error("navigate_to requires a url");
    // Use the first existing tab directly — do NOT call ensureAgentTab which
    // would create a second about:blank tab if no web URL tab exists yet.
    const allTabs = await chrome.tabs.query({});
    let tab = allTabs[0];
    if (!tab) {
      // Edge case: no tabs at all (should not happen with about:blank launch).
      const created = await chrome.tabs.create({ url: startupUrl, active: true });
      tab = created;
    } else {
      await chrome.tabs.update(tab.id, { url: startupUrl, active: true });
    }
    state.agentTabId = tab.id;
    if (isWebUrl(startupUrl)) state.lastSafeUrls.set(tab.id, startupUrl);
    // Wait for content script ready (up to 15s).
    for (let i = 0; i < 30; i++) {
      if (state.contentReady.has(tab.id)) break;
      await new Promise((r) => setTimeout(r, 500));
    }
    // Apply the controlling indicator + cursor.
    state.desiredIndicator[tab.id] = "controlling";
    try { await setIndicatorAck(tab.id, "controlling"); } catch {}
    try {
      await sendToTabAck(tab.id, { type: "karox-move-cursor", x: 720, y: 450, action: "" });
      state.cursorPresent[tab.id] = true;
    } catch {}
    return {
      navigated: true,
      tab_id: tabRef(tab.id),
      url: safeUrl(startupUrl),
      content_ready: state.contentReady.has(tab.id),
    };
  }
  if (method === "show_cursor") {
    // Show the agent cursor at the viewport centre without a DOM action.
    // Used at startup so the user sees the cursor immediately (Point A).
    const tab = await currentAgentTab();
    const x = Number(params.x) || 720;
    const y = Number(params.y) || 450;
    try {
      await sendToTabAck(tab.id, { type: "karox-move-cursor", x, y, action: "" });
      state.cursorPresent[tab.id] = true;
      return { shown: true, tab_id: tabRef(tab.id), x, y };
    } catch (err) {
      return { shown: false, tab_id: tabRef(tab.id), error: String(err).slice(0, 200) };
    }
  }
  if (method === "setup_startup_tab") {
    // B6: Chrome for Testing may create an extra about:blank tab even when a
    // startup URL is provided. This method waits for the startup URL tab to
    // load, closes all other tabs, sets the agent tab, activates it, and
    // applies the indicator + cursor. Called once from _launch_fresh.
    const startupUrl = String(params.url || "");
    const startupHost = startupUrl.replace(/^https?:\/\//, "").split("/")[0];
    if (!startupHost) throw new Error("setup_startup_tab requires a url");
    // Wait up to 15s for a tab with the startup URL to appear (url or pendingUrl).
    let startupTab = null;
    for (let attempt = 0; attempt < 30; attempt++) {
      const tabs = await chrome.tabs.query({});
      startupTab = tabs.find((t) =>
        (t.url || "").includes(startupHost) ||
        (t.pendingUrl || "").includes(startupHost),
      );
      if (startupTab) break;
      await new Promise((r) => setTimeout(r, 500));
    }
    if (!startupTab) throw new Error(`startup URL tab not found after 15s (host=${startupHost})`);
    // Close all other tabs (about:blank, etc.).
    const allTabs = await chrome.tabs.query({});
    const otherTabs = allTabs.filter((t) => t.id !== startupTab.id);
    for (const t of otherTabs) {
      try { await chrome.tabs.remove(t.id); } catch {}
      state.lastSafeUrls.delete(t.id);
      delete state.desiredIndicator[t.id];
      delete state.lastOverlayAck[t.id];
      delete state.cursorPresent[t.id];
      state.contentReady.delete(t.id);
    }
    // Set as agent tab and activate.
    state.agentTabId = startupTab.id;
    if (isWebUrl(startupUrl)) state.lastSafeUrls.set(startupTab.id, startupUrl);
    await chrome.tabs.update(startupTab.id, { active: true });
    // Apply the controlling indicator.
    state.desiredIndicator[startupTab.id] = "controlling";
    try { await setIndicatorAck(startupTab.id, "controlling"); } catch {}
    // Show cursor at viewport centre.
    try {
      await sendToTabAck(startupTab.id, { type: "karox-move-cursor", x: 720, y: 450, action: "" });
      state.cursorPresent[startupTab.id] = true;
    } catch {}
    return {
      setup: true,
      tab_id: tabRef(startupTab.id),
      url: safeUrl(startupTab.url || startupUrl),
      closed_extra_tabs: otherTabs.length,
    };
  }
  throw new Error(`unknown command: ${method}`);
}

chrome.webNavigation.onCommitted.addListener((details) => {
  if (details.frameId !== 0) return;
  if (isWebUrl(details.url)) state.lastSafeUrls.set(details.tabId, details.url);
  // After navigation/reload, the manifest re-injects content.js. When content.js
  // sends karox-content-ready, the service worker re-applies the stored desired
  // indicator state. This listener also does a delayed re-apply as a fallback
  // for cases where the ready message was missed.
  if (details.tabId === state.agentTabId) {
    const desired = state.desiredIndicator[details.tabId] || (state.takeover ? "takeover" : "controlling");
    state.desiredIndicator[details.tabId] = desired;
    setTimeout(() => {
      setIndicatorAck(details.tabId, desired).catch(() => {
        // Will be retried when content.js sends karox-content-ready.
      });
    }, 300);
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  state.lastSafeUrls.delete(tabId);
  delete state.desiredIndicator[tabId];
  delete state.lastOverlayAck[tabId];
  delete state.cursorPresent[tabId];
  state.contentReady.delete(tabId);
  if (state.agentTabId === tabId) state.agentTabId = null;
});

chrome.webRequest.onBeforeRequest.addListener(
  (details) => state.requestStarted.set(details.requestId, performance.now()),
  { urls: ["<all_urls>"] },
);

chrome.webRequest.onCompleted.addListener(
  (details) => {
    const started = state.requestStarted.get(details.requestId);
    state.requestStarted.delete(details.requestId);
    state.network.push({
      url: safeUrl(details.url),
      method: details.method,
      status: details.statusCode,
      resource_type: details.type,
      duration_ms: started == null ? null : Math.round((performance.now() - started) * 100) / 100,
      tab_id: details.tabId >= 0 ? tabRef(details.tabId) : null,
      error: null,
      field_names: [],
      values: {},
    });
    trimNetwork();
  },
  { urls: ["<all_urls>"] },
);

chrome.webRequest.onErrorOccurred.addListener(
  (details) => {
    const started = state.requestStarted.get(details.requestId);
    state.requestStarted.delete(details.requestId);
    state.network.push({
      url: safeUrl(details.url),
      method: details.method,
      status: 0,
      resource_type: details.type,
      duration_ms: started == null ? null : Math.round((performance.now() - started) * 100) / 100,
      tab_id: details.tabId >= 0 ? tabRef(details.tabId) : null,
      error: String(details.error || "request failed").slice(0, 300),
      field_names: [],
      values: {},
    });
    trimNetwork();
  },
  { urls: ["<all_urls>"] },
);

// Page-console relay from content scripts. We never trust the shape: every
// field is coerced and length-capped, and only our own content-script namespace
// is accepted (sender.id === chrome.runtime.id guarantees a same-extension
// sender, not the page itself).
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "karox-console" && sender.id === chrome.runtime.id) {
    const tabId = sender.tab?.id;
    if (tabId == null) {
      sendResponse({ ok: true });
      return true;
    }
    if (state.console.length >= MAX_CONSOLE) state.console.shift();
    state.console.push({
      type: String(message.level || "log").slice(0, 20),
      text: String(message.text || "").slice(0, 1200),
      tab_id: tabRef(tabId),
    });
    trimConsole();
    sendResponse({ ok: true });
    return true;
  }
  if (message?.type === "karox-content-ready" && sender.id === chrome.runtime.id) {
    const tabId = sender.tab?.id;
    if (tabId != null) {
      state.contentReady.add(tabId);
      // Re-apply the desired indicator state for this tab (survives navigation/reload).
      const desired = state.desiredIndicator[tabId];
      if (desired) {
        setIndicatorAck(tabId, desired).catch(() => {
          // Failed to re-apply; will retry on next content-ready or agent action.
        });
      }
    }
    sendResponse({ ok: true });
    return true;
  }
  if (message?.type === "karox-panel-state") {
    sendResponse({
      connected: state.connected,
      takeover: state.takeover,
      sessionId: state.sessionId,
      agentTabId: state.agentTabId ? tabRef(state.agentTabId) : null,
    });
    return true;
  }
  if (message?.type === "karox-panel-takeover") {
    dispatchCommand(message.enabled ? "takeover" : "resume", {})
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }
  if (message?.type === "karox-panel-tabs") {
    listPublicTabs()
      .then((tabs) => sendResponse({ ok: true, tabs }))
      .catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }
  return false;
});

chrome.action.onClicked.addListener(async (tab) => {
  try {
    await chrome.sidePanel.open({ windowId: tab.windowId });
  } catch {}
});

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
connectBridge().catch(() => scheduleReconnect());
