const MAX_NETWORK = 400;
const MAX_CONSOLE = 200;
const PING_MS = 20000;

const state = {
  socket: null,
  reconnectTimer: null,
  pingTimer: null,
  connected: false,
  takeover: false,
  agentTabId: null,
  brandingTabId: null,
  sessionId: null,
  lastSafeUrls: new Map(),
  requestStarted: new Map(),
  network: [],
  console: [],
};

function tabRef(tabId) {
  return `tab-${tabId}`;
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

function isBrandingUrl(url) {
  return typeof url === "string" && (
    url.startsWith(chrome.runtime.getURL("newtab.html")) ||
    url === "chrome://newtab/"
  );
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
  state.sessionId = config.session_id || null;
  const socket = new WebSocket(`${config.websocket_url}?token=${encodeURIComponent(config.token)}`);
  state.socket = socket;

  socket.onopen = async () => {
    state.connected = true;
    socket.send(JSON.stringify({
      type: "hello",
      extension_version: chrome.runtime.getManifest().version,
      session_id: state.sessionId,
      browser: navigator.userAgent,
    }));
    clearInterval(state.pingTimer);
    state.pingTimer = setInterval(() => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "ping", at: Date.now() }));
      }
    }, PING_MS);
    await setBadge();
  };

  socket.onmessage = async (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (!message || message.type !== "command" || typeof message.id !== "string") return;
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
    scheduleReconnect();
  };

  socket.onerror = () => {
    try { socket.close(); } catch {}
  };
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
    if (existing && isWebUrl(existing.url || "")) return existing;
  }
  const tabs = await allTabs();
  const candidate = tabs.find((tab) => isWebUrl(tab.url || ""));
  if (candidate) {
    state.agentTabId = candidate.id;
    return candidate;
  }
  const created = await chrome.tabs.create({ url: "about:blank", active: false });
  state.agentTabId = created.id;
  return created;
}

async function ensureBrandingTab() {
  const tabs = await allTabs();
  const found = tabs.find((tab) => isBrandingUrl(tab.url || ""));
  if (found) {
    state.brandingTabId = found.id;
    return found;
  }
  const created = await chrome.tabs.create({ url: chrome.runtime.getURL("newtab.html"), active: false, pinned: true });
  state.brandingTabId = created.id;
  return created;
}

function snapshotPage() {
  const secretHint = /password|secret|token|api[_-]?key|credential|cookie|card|cvv|cvc/i;
  const out = { headings: [], buttons: [], inputs: [], links: [], dialogs: [], tabs: [], text: "" };
  const collected = [];
  const nodes = Array.from(document.querySelectorAll(
    "h1,h2,h3,h4,h5,h6,button,input,select,textarea,a,[role=button],[role=dialog],[role=tab]"
  )).slice(0, 700);
  for (const el of nodes) {
    const tag = el.tagName.toLowerCase();
    const label = (el.getAttribute("aria-label") || el.innerText || el.placeholder || "").trim().slice(0, 240);
    if (/^h[1-6]$/.test(tag)) out.headings.push({ level: tag, text: label });
    else if (tag === "button" || el.getAttribute("role") === "button") {
      out.buttons.push({ name: label, disabled: Boolean(el.disabled) });
    } else if (["input", "select", "textarea"].includes(tag)) {
      const type = (el.getAttribute("type") || tag).toLowerCase();
      const secret = type === "password" || secretHint.test([el.name, el.id, label].join(" "));
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

function domAction(payload) {
  const visible = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity || 1) > 0 && rect.width >= 0 && rect.height >= 0;
  };
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
  const resolve = (selector) => {
    if (selector.startsWith("text=")) return textTarget(selector.slice(5));
    if (selector.startsWith("label=")) return labelTarget(selector.slice(6));
    if (selector.startsWith("role=")) return roleTarget(selector.slice(5));
    try { return document.querySelector(selector); } catch { return null; }
  };
  const el = resolve(String(payload.selector || ""));
  if (!el) throw new Error("element not found");
  const scope = el.closest("form,section,article,[role=dialog]");
  const metadata = {
    type: String(el.getAttribute("type") || el.tagName || "").toLowerCase(),
    name: String(el.getAttribute("name") || ""),
    id: String(el.id || ""),
    aria: String(el.getAttribute("aria-label") || ""),
    text: String(el.innerText || el.value || "").trim().slice(0, 500),
    context: String(scope?.innerText || "").trim().slice(0, 2200),
    disabled: Boolean(el.disabled),
    visible: visible(el),
  };
  if (payload.action === "inspect") return metadata;
  if (payload.action === "click") {
    el.scrollIntoView({ block: "center", inline: "center" });
    el.focus({ preventScroll: true });
    el.click();
    return { clicked: true, metadata };
  }
  if (payload.action === "fill") {
    el.focus({ preventScroll: true });
    const value = String(payload.value ?? "");
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value); else el.value = value;
    el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { filled: true, value_length: value.length, metadata };
  }
  if (payload.action === "select") {
    const values = Array.isArray(payload.value) ? payload.value.map(String) : [String(payload.value)];
    for (const option of el.options || []) option.selected = values.includes(option.value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { selected: true, count: values.length, metadata };
  }
  if (payload.action === "press") {
    const key = String(payload.key || "");
    el.focus({ preventScroll: true });
    el.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
    el.dispatchEvent(new KeyboardEvent("keyup", { key, bubbles: true }));
    if (key.toLowerCase() === "enter") {
      const form = el.closest("form");
      if (form?.requestSubmit) form.requestSubmit();
    } else if (key === " " || key.toLowerCase() === "space") {
      el.click();
    }
    return { pressed: true, key, metadata };
  }
  if (payload.action === "text") {
    return { text: String(el.innerText || el.textContent || el.value || "").slice(0, 30000), metadata };
  }
  throw new Error("unsupported DOM action");
}

async function executeScript(tabId, func, args = []) {
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
  return executeScript(tabId, domAction, [{ action, ...params }]);
}

async function currentAgentTab() {
  const tab = await ensureAgentTab();
  if (!tab?.id) throw new Error("no KaroX work tab is available");
  return tab;
}

async function listPublicTabs() {
  await ensureBrandingTab();
  const tabs = await allTabs();
  return tabs.map((tab) => ({
    tab_id: tabRef(tab.id),
    active: tab.id === state.agentTabId,
    browser_active: Boolean(tab.active),
    protected: tab.id === state.brandingTabId,
    url: safeUrl(tab.url || ""),
    title: String(tab.title || "").slice(0, 300),
    window_id: tab.windowId,
  }));
}

async function dispatchCommand(method, params) {
  if (method === "status") {
    return { connected: state.connected, takeover: state.takeover, session_id: state.sessionId };
  }
  if (method === "open") {
    let tab = await currentAgentTab();
    tab = await chrome.tabs.update(tab.id, { url: String(params.url), active: false });
    state.agentTabId = tab.id;
    if (isWebUrl(params.url)) state.lastSafeUrls.set(tab.id, String(params.url));
    return { open: true, tab_id: tabRef(tab.id), url: safeUrl(params.url), background: true };
  }
  if (method === "tabs") {
    const tabs = await listPublicTabs();
    return {
      tabs,
      count: tabs.length,
      active_tab_id: state.agentTabId ? tabRef(state.agentTabId) : null,
      branding_tab_id: state.brandingTabId ? tabRef(state.brandingTabId) : null,
      takeover_active: state.takeover,
    };
  }
  if (method === "new_tab") {
    const tab = await chrome.tabs.create({ url: params.url || "about:blank", active: false });
    state.agentTabId = tab.id;
    if (isWebUrl(params.url || "")) state.lastSafeUrls.set(tab.id, String(params.url));
    return { created: true, tab_id: tabRef(tab.id), url: safeUrl(tab.url || params.url || ""), background: true };
  }
  if (method === "switch_tab") {
    const tabId = parseTabRef(params.tab_id);
    const tab = await getTab(tabId);
    if (!tab) throw new Error("tab does not exist");
    state.agentTabId = tabId;
    return { switched: true, tab_id: tabRef(tabId), url: safeUrl(tab.url || ""), background: true };
  }
  if (method === "close_tab") {
    const tabId = parseTabRef(params.tab_id);
    if (tabId === state.brandingTabId) throw new Error("the protected KaroX tab cannot be closed");
    const workTabs = (await allTabs()).filter((tab) => tab.id !== state.brandingTabId);
    if (workTabs.length <= 1) throw new Error("cannot close the last work tab");
    await chrome.tabs.remove(tabId);
    state.lastSafeUrls.delete(tabId);
    if (state.agentTabId === tabId) {
      const next = workTabs.find((tab) => tab.id !== tabId);
      state.agentTabId = next?.id || null;
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
  if (["click", "fill", "select", "press", "get_text"].includes(method)) {
    if (state.takeover) throw new Error("user takeover is active");
    const tab = await currentAgentTab();
    const action = method === "get_text" ? "text" : method;
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
    const previous = (await chrome.tabs.query({ active: true, windowId: tab.windowId }))[0];
    await chrome.tabs.update(tab.id, { active: true });
    const data_url = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
    if (previous?.id && previous.id !== tab.id) await chrome.tabs.update(previous.id, { active: true });
    return { data_url, tab_id: tabRef(tab.id), full_page: false, viewport_only: true };
  }
  if (method === "network") {
    return { requests: state.network.slice(), count: state.network.length };
  }
  if (method === "console") {
    return { entries: state.console.slice(), count: state.console.length, supported: false, note: "page console capture is not enabled in extension mode" };
  }
  if (method === "takeover") {
    const tab = await currentAgentTab();
    state.takeover = true;
    await chrome.tabs.update(tab.id, { active: true });
    await chrome.windows.update(tab.windowId, { focused: true });
    await setBadge();
    return { takeover: true, agent_input_paused: true, tab_id: tabRef(tab.id), url: safeUrl(tab.url || "") };
  }
  if (method === "resume") {
    state.takeover = false;
    await setBadge();
    const tab = await currentAgentTab();
    return { resumed: true, agent_input_paused: false, tab_id: tabRef(tab.id), url: safeUrl(tab.url || "") };
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
  throw new Error(`unknown command: ${method}`);
}

chrome.webNavigation.onCommitted.addListener((details) => {
  if (details.frameId !== 0) return;
  if (isWebUrl(details.url)) state.lastSafeUrls.set(details.tabId, details.url);
});

chrome.tabs.onRemoved.addListener((tabId) => {
  state.lastSafeUrls.delete(tabId);
  if (state.agentTabId === tabId) state.agentTabId = null;
  if (state.brandingTabId === tabId) state.brandingTabId = null;
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

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
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
ensureBrandingTab().catch(() => {});
connectBridge().catch(() => scheduleReconnect());
