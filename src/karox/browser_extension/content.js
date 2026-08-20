"use strict";
(() => {
  // KaroX in-page overlay (ISOLATED world). Runs only inside an isolated
  // content-script world; the page's MAIN world talks to us through a namespaced
  // window.postMessage channel that we validate strictly. All overlay visuals
  // live inside a closed Shadow DOM so the page cannot read, restyle, click, or
  // select them, and they never enter the page's accessibility tree or layout.

  const CONSOLE_NS = "__karox_console_bridge__";
  const HOST_ATTR = "data-karox-overlay-host";
  const CURSOR_TIMEOUT_MS = 400;
  const CURSOR_MIN_DURATION_MS = 90;
  const CURSOR_MAX_DURATION_MS = 180;
  const RESUMED_HOLD_MS = 1500;

  if (window.__karoxContentInit) return;
  window.__karoxContentInit = true;

  // Tell the service worker that content.js is ready. The service worker stores
  // the desired indicator state per tab and re-applies it on ready/navigation.
  try {
    chrome.runtime.sendMessage({ type: "karox-content-ready", at: Date.now() }, function () {
      void chrome.runtime.lastError;
    });
  } catch {
    // Service worker may be gone; page is unaffected.
  }

  let host = null;
  let shadow = null;
  let cursorEl = null;
  let indicatorEl = null;
  let indicatorText = null;
  let cursor = { x: -200, y: -200, visible: false };
  let rafId = null;
  let guardTimer = null;
  let resumedTimer = null;
  let indicatorShown = false;

  const OVERLAY_CSS =
    ":host{all:initial}" +
    "style{display:none!important}" +
    "#cursor,#cursor *,#indicator,#indicator *{box-sizing:border-box}" +
    "#cursor{position:fixed;top:0;left:0;width:0;height:0;pointer-events:none;" +
    "z-index:2147483647;will-change:transform}" +
    "#cursor.on #cursorShape{opacity:1}" +
    "#cursorShape{position:absolute;left:-1px;top:-1px;width:22px;height:22px;opacity:0;" +
    "transition:opacity 90ms ease-out;filter:drop-shadow(0 1px 1px rgba(255,255,255,.42)) " +
    "drop-shadow(0 2px 4px rgba(0,0,0,.42))}" +
    "#cursorShape svg{display:block;width:22px;height:22px;overflow:visible}" +
    "#clickHalo{position:absolute;left:2px;top:2px;width:18px;height:18px;border-radius:999px;" +
    "border:1.5px solid rgba(113,170,255,.9);opacity:0;transform:translate(-50%,-50%) scale(.72);" +
    "box-shadow:0 0 0 1px rgba(255,255,255,.4) inset}" +
    "#cursor.pulse #clickHalo{animation:karox-click .34s cubic-bezier(.16,1,.3,1)}" +
    "@keyframes karox-click{0%{opacity:.78;transform:translate(-50%,-50%) scale(.58)}" +
    "70%{opacity:.26}100%{opacity:0;transform:translate(-50%,-50%) scale(1.45)}}" +
    "#indicator{position:fixed;top:12px;right:12px;pointer-events:none;z-index:2147483647;" +
    "display:none;align-items:center;gap:7px;opacity:0;transform:translateY(-3px) scale(.985);" +
    "transition:opacity 140ms ease-out,transform 180ms cubic-bezier(.16,1,.3,1);" +
    "font-family:Inter,ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif;" +
    "font-size:12px;font-weight:560;letter-spacing:-.005em;line-height:1;color:rgba(255,255,255,.94);" +
    "background:rgba(24,24,27,.86);-webkit-backdrop-filter:blur(14px) saturate(1.18);" +
    "backdrop-filter:blur(14px) saturate(1.18);border:1px solid rgba(255,255,255,.14);" +
    "border-radius:999px;padding:6px 9px 6px 6px;white-space:nowrap;" +
    "box-shadow:0 6px 18px rgba(0,0,0,.2),0 1px 2px rgba(0,0,0,.16);" +
    "max-width:calc(100vw - 24px)}" +
    "#indicator.on{opacity:1;transform:translateY(0) scale(1)}" +
    "#indicator .statusIcon{display:grid;place-items:center;width:18px;height:18px;border-radius:999px;" +
    "background:rgba(126,167,255,.14);color:#9bbcff;flex:0 0 auto}" +
    "#indicator .statusIcon svg{display:block;width:11px;height:11px}" +
    "#indicator.takeover .statusIcon{background:rgba(244,180,88,.14);color:#f2bd73}" +
    "#indicator.resumed .statusIcon{background:rgba(94,210,154,.14);color:#7bd8ad}" +
    "#indicator #itxt{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}" +
    "@media(prefers-reduced-motion:reduce){#cursorShape,#indicator{transition:none!important}" +
    "#cursor.pulse #clickHalo{animation:none!important}}";

  const CURSOR_SVG =
    '<svg viewBox="0 0 22 22" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">' +
    '<path d="M2.35 1.55 2.4 17.8l4.05-3.6 3.05 6.25 3.05-1.5-3.02-6.08 5.55-.28Z" ' +
    'fill="#17181c" stroke="rgba(255,255,255,.96)" stroke-width="1.15" stroke-linejoin="round"/></svg>';

  const STATUS_SVG =
    '<svg viewBox="0 0 12 12" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">' +
    '<path d="M6 1.2c.35 2.35 1.25 3.25 3.6 3.6C7.25 5.15 6.35 6.05 6 8.4 5.65 6.05 4.75 5.15 2.4 4.8 4.75 4.45 5.65 3.55 6 1.2Z" fill="currentColor"/>' +
    '<circle cx="9.35" cy="8.95" r="1.25" fill="currentColor" opacity=".72"/></svg>';

  function ensureOverlay() {
    if (host && host.isConnected) return;
    host = document.createElement("div");
    host.setAttribute(HOST_ATTR, "");
    host.setAttribute("aria-hidden", "true");
    host.style.cssText =
      "all:initial;position:fixed;top:0;left:0;width:0;height:0;" +
      "pointer-events:none;z-index:2147483647";
    shadow = host.attachShadow({ mode: "closed" });
    // Insert CSS via a real HTMLStyleElement — never via shadow.innerHTML with
    // a raw CSS string, which some Chromium builds render as text content.
    var styleEl = document.createElement("style");
    styleEl.textContent = OVERLAY_CSS;
    shadow.appendChild(styleEl);
    // Cursor: compact OS-like pointer plus a separate click halo. The pointer
    // itself never scales on click, so the hotspot stays visually stable.
    var cursorWrap = document.createElement("div");
    cursorWrap.id = "cursor";
    cursorWrap.setAttribute("aria-hidden", "true");
    var clickHalo = document.createElement("div");
    clickHalo.id = "clickHalo";
    cursorWrap.appendChild(clickHalo);
    var cursorShape = document.createElement("div");
    cursorShape.id = "cursorShape";
    cursorShape.innerHTML = CURSOR_SVG;
    cursorWrap.appendChild(cursorShape);
    shadow.appendChild(cursorWrap);
    // Indicator: compact non-blocking status chip in the safe top-right corner.
    var indicatorWrap = document.createElement("div");
    indicatorWrap.id = "indicator";
    indicatorWrap.setAttribute("aria-hidden", "true");
    var statusIcon = document.createElement("span");
    statusIcon.className = "statusIcon";
    statusIcon.innerHTML = STATUS_SVG;
    indicatorWrap.appendChild(statusIcon);
    indicatorText = document.createElement("span");
    indicatorText.id = "itxt";
    indicatorWrap.appendChild(indicatorText);
    shadow.appendChild(indicatorWrap);
    cursorEl = cursorWrap;
    indicatorEl = indicatorWrap;
    // documentElement is available even before <body> is parsed.
    (document.documentElement || document.body).appendChild(host);
  }

  function showCursor() {
    if (!cursorEl) return;
    cursor.visible = true;
    cursorEl.classList.add("on");
  }

  function hideCursor() {
    if (!cursorEl) return;
    cursor.visible = false;
    cursorEl.classList.remove("on");
  }

  function moveCursor(targetX, targetY, actionLabel) {
    ensureOverlay();
    if (rafId) cancelAnimationFrame(rafId);
    if (guardTimer) clearTimeout(guardTimer);
    const reduce =
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const hasPosition = cursor.x >= 0 && cursor.y >= 0;
    const startX = hasPosition ? cursor.x : targetX;
    const startY = hasPosition ? cursor.y : targetY;
    const dx = targetX - startX;
    const dy = targetY - startY;
    const distance = Math.hypot(dx, dy);
    const duration = Math.min(
      CURSOR_MAX_DURATION_MS,
      Math.max(CURSOR_MIN_DURATION_MS, 78 + distance * 0.16),
    );
    showCursor();

    return new Promise((resolve) => {
      let settled = false;
      function done() {
        if (settled) return;
        settled = true;
        if (rafId) cancelAnimationFrame(rafId);
        rafId = null;
        if (guardTimer) clearTimeout(guardTimer);
        guardTimer = null;
        cursor.x = targetX;
        cursor.y = targetY;
        if (cursorEl) {
          cursorEl.style.transform =
            "translate3d(" + targetX + "px," + targetY + "px,0)";
        }
        if (actionLabel && cursorEl) {
          cursorEl.classList.remove("pulse");
          // Force a clean restart when two actions happen in quick succession.
          void cursorEl.offsetWidth;
          cursorEl.classList.add("pulse");
          setTimeout(
            () => {
              if (cursorEl) cursorEl.classList.remove("pulse");
            },
            360,
          );
        }
        resolve();
      }

      // Never animate the first appearance from the synthetic off-screen origin.
      // Reduced-motion users also get an immediate, stable pointer.
      if (reduce || !hasPosition || distance < 3) {
        done();
        return;
      }

      const start = performance.now();
      function step(now) {
        const t = Math.min(1, (now - start) / duration);
        // Cubic ease-out: responsive near the start, gentle at the target.
        const e = 1 - Math.pow(1 - t, 3);
        cursor.x = startX + dx * e;
        cursor.y = startY + dy * e;
        cursorEl.style.transform =
          "translate3d(" + cursor.x + "px," + cursor.y + "px,0)";
        if (t < 1) {
          rafId = requestAnimationFrame(step);
        } else {
          done();
        }
      }
      // Safety fallback: never let a hung rAF block the pending DOM action.
      guardTimer = setTimeout(done, CURSOR_TIMEOUT_MS);
      rafId = requestAnimationFrame(step);
    });
  }

  function setIndicator(state) {
    ensureOverlay();
    if (resumedTimer) {
      clearTimeout(resumedTimer);
      resumedTimer = null;
    }
    indicatorEl.classList.remove("takeover", "resumed");
    if (state === "controlling") {
      indicatorText.textContent = "KaroX управляет этой вкладкой";
      indicatorEl.classList.add("on");
      indicatorShown = true;
    } else if (state === "takeover") {
      indicatorText.textContent = "Управление передано вам";
      indicatorEl.classList.add("takeover", "on");
      indicatorShown = true;
      // Cursor must not move or appear while the user owns the tab.
      hideCursor();
    } else if (state === "resumed") {
      indicatorText.textContent = "KaroX продолжил работу";
      indicatorEl.classList.add("resumed", "on");
      indicatorShown = true;
      // After the transient "resumed" hint, restore the long-lived controlling state.
      resumedTimer = setTimeout(function () {
        setIndicator("controlling");
      }, RESUMED_HOLD_MS);
    } else {
      // "hidden" or unknown: hide cleanly.
      indicatorEl.classList.remove("on");
      indicatorShown = false;
    }
  }

  function removeOverlay() {
    if (rafId) cancelAnimationFrame(rafId);
    rafId = null;
    if (guardTimer) clearTimeout(guardTimer);
    guardTimer = null;
    if (resumedTimer) clearTimeout(resumedTimer);
    resumedTimer = null;
    if (host && host.isConnected) host.remove();
    host = null;
    shadow = null;
    cursorEl = null;
    indicatorEl = null;
    indicatorText = null;
    cursor = { x: -200, y: -200, visible: false };
    indicatorShown = false;
  }

  // page-console relay (MAIN -> ISOLATED -> service worker). Only the namespaced
  // channel is accepted; any other postMessage on the page is ignored.
  window.addEventListener("message", function (event) {
    if (event.source !== window) return;
    const data = event.data;
    if (
      !data ||
      typeof data !== "object" ||
      data.ns !== CONSOLE_NS ||
      data.type !== "console"
    ) {
      return;
    }
    try {
      chrome.runtime.sendMessage(
        {
          type: "karox-console",
          level: String(data.level || "").slice(0, 20),
          text: String(data.text || "").slice(0, 1200),
          at: Number(data.at) || Date.now(),
        },
        function () {
          void chrome.runtime.lastError;
        },
      );
    } catch {
      // Bridge/worker may be gone; page console is unaffected because the
      // MAIN-world bridge always calls the original methods.
    }
  });

  // Commands from the service worker.
  chrome.runtime.onMessage.addListener(function (msg, _sender, sendResponse) {
    if (!msg || typeof msg !== "object") return false;
    try {
      if (msg.type === "karox-move-cursor") {
        const x = Number(msg.x) || 0;
        const y = Number(msg.y) || 0;
        moveCursor(x, y, msg.action || "")
          .then(function () {
            sendResponse({ ok: true });
          })
          .catch(function (err) {
            sendResponse({
              ok: false,
              error: String((err && err.message) || err).slice(0, 200),
            });
          });
        return true; // async sendResponse
      }
      if (msg.type === "karox-set-indicator") {
        setIndicator(String(msg.state || "hidden"));
        sendResponse({ ok: true });
        return false;
      }
      if (msg.type === "karox-hide-cursor") {
        hideCursor();
        sendResponse({ ok: true });
        return false;
      }
      if (msg.type === "karox-remove-overlay") {
        removeOverlay();
        sendResponse({ ok: true });
        return false;
      }
      if (msg.type === "karox-ping") {
        sendResponse({ ok: true, alive: true, indicator: indicatorShown });
        return false;
      }
      if (msg.type === "karox-get-state") {
        sendResponse({
          ok: true,
          overlay_present: !!(host && host.isConnected),
          indicator_state: indicatorShown ? (indicatorEl.classList.contains("takeover") ? "takeover" : indicatorEl.classList.contains("resumed") ? "resumed" : "controlling") : "hidden",
          cursor_present: cursor.visible,
          content_script_ready: true,
          indicator_text: indicatorText ? indicatorText.textContent : null,
          document_url: location.href,
          document_title: document.title,
        });
        return false;
      }
      if (msg.type === "karox-get-geometry") {
        // Detailed rendered geometry for visual verification.
        // Checks actual computed style + bounding rect, not just .isConnected.
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        const result = {
          ok: true,
          viewport: { width: vw, height: vh },
          host_present: !!(host && host.isConnected),
          host_rect: null,
          indicator_rect: null,
          indicator_computed_style: null,
          indicator_has_on_class: false,
          cursor_rect: null,
          cursor_computed_style: null,
          cursor_has_on_class: false,
          rendered_visible: false,
        };
        if (host && host.isConnected) {
          result.host_rect = host.getBoundingClientRect().toJSON
            ? host.getBoundingClientRect().toJSON()
            : (function (r) { return { x: r.x, y: r.y, width: r.width, height: r.height, top: r.top, bottom: r.bottom, left: r.left, right: r.right }; })(host.getBoundingClientRect());
          // CSS leak detection: verify no raw CSS text leaked into the document.
          var bodyText = "";
          try { bodyText = (document.body && document.body.innerText) || ""; } catch {}
          result.css_leak_in_body = /#cursor\s*\{|#indicator\s*\{|@keyframes\s+karox/.test(bodyText);
          result.shadow_has_style_element = false;
          result.host_has_text_children = false;
          try {
            // closed shadow — cannot inspect directly, but we can check host
            for (var i = 0; i < host.childNodes.length; i++) {
              if (host.childNodes[i].nodeType === 3) { // Text node
                result.host_has_text_children = true;
                break;
              }
            }
          } catch {}
        }
        if (indicatorEl) {
          const r = indicatorEl.getBoundingClientRect();
          result.indicator_rect = { x: r.x, y: r.y, width: r.width, height: r.height, top: r.top, bottom: r.bottom, left: r.left, right: r.right };
          const cs = shadow ? getComputedStyle(indicatorEl) : null;
          if (cs) {
            result.indicator_computed_style = {
              display: cs.display,
              visibility: cs.visibility,
              opacity: cs.opacity,
              zIndex: cs.zIndex,
              position: cs.position,
            };
          }
          result.indicator_has_on_class = indicatorEl.classList.contains("on");
          result.indicator_text = indicatorText ? indicatorText.textContent : null;
        }
        if (cursorEl) {
          // cursorEl (#cursor) has width:0;height:0; the visible shape is
          // #cursorShape inside it. Use cursorShape rect for geometry.
          var shapeRect = null;
          try {
            var shapeEl = shadow ? shadow.querySelector("#cursorShape") : null;
            if (shapeEl) shapeRect = shapeEl.getBoundingClientRect();
          } catch {}
          var r = shapeRect || cursorEl.getBoundingClientRect();
          result.cursor_rect = { x: r.x, y: r.y, width: r.width, height: r.height, top: r.top, bottom: r.bottom, left: r.left, right: r.right };
          var csEl = shapeRect ? (shadow ? shadow.querySelector("#cursorShape") : null) : cursorEl;
          const cs = csEl ? getComputedStyle(csEl) : null;
          if (cs) {
            result.cursor_computed_style = {
              display: cs.display,
              visibility: cs.visibility,
              opacity: cs.opacity,
              zIndex: cs.zIndex,
              position: cs.position,
            };
          }
          result.cursor_has_on_class = cursorEl.classList.contains("on");
        }
        // rendered_visible: indicator is on, has non-zero rect inside viewport, opacity > 0
        var indVis = false;
        if (result.indicator_rect && result.indicator_computed_style) {
          var ir = result.indicator_rect;
          var ic = result.indicator_computed_style;
          indVis = result.indicator_has_on_class
            && ir.width > 0 && ir.height > 0
            && ir.bottom > 0 && ir.top < vh
            && ir.right > 0 && ir.left < vw
            && ic.display !== "none"
            && ic.visibility !== "hidden"
            && parseFloat(ic.opacity) > 0;
        }
        var curVis = false;
        if (result.cursor_rect && result.cursor_computed_style) {
          var cr = result.cursor_rect;
          var cc = result.cursor_computed_style;
          curVis = result.cursor_has_on_class
            && cr.width > 0 && cr.height > 0
            && cr.bottom > 0 && cr.top < vh
            && cr.right > 0 && cr.left < vw
            && cc.display !== "none"
            && cc.visibility !== "hidden"
            && parseFloat(cc.opacity) > 0;
        }
        // Runtime invariants: fully inside viewport with 8px margin
        result.indicator_fully_inside_viewport = false;
        if (result.indicator_rect) {
          var ir2 = result.indicator_rect;
          result.indicator_fully_inside_viewport =
            ir2.left >= 8 && ir2.right <= vw - 8 &&
            ir2.top >= 8 && ir2.bottom <= vh - 8 &&
            ir2.width > 0 && ir2.height > 0;
        }
        result.cursor_fully_inside_viewport = false;
        if (result.cursor_rect) {
          var cr2 = result.cursor_rect;
          result.cursor_fully_inside_viewport =
            cr2.left >= 8 && cr2.right <= vw - 8 &&
            cr2.top >= 8 && cr2.bottom <= vh - 8 &&
            cr2.width > 0 && cr2.height > 0;
        }
        // Cursor effective fill/border for visibility evidence
        var shapeEl = shadow ? shadow.querySelector("#cursorShape") : null;
        if (shapeEl) {
          var scs = getComputedStyle(shapeEl);
          result.cursor_effective_opacity = scs.opacity;
          result.cursor_effective_filter = scs.filter;
          result.cursor_pixel_area = (result.cursor_rect ? result.cursor_rect.width * result.cursor_rect.height : 0);
        }
        var svgEl = shadow ? shadow.querySelector("#cursorShape svg") : null;
        if (svgEl) {
          var pathEl = svgEl.querySelector("path");
          if (pathEl) {
            var pcs = getComputedStyle(pathEl);
            result.cursor_effective_fill = pcs.fill;
            result.cursor_effective_stroke = pcs.stroke;
          }
        }
        result.rendered_visible = indVis && curVis;
        result.indicator_visible = indVis;
        result.cursor_visible = curVis;
        result.cursor_rendered_visible = curVis;
        sendResponse(result);
        return false;
      }
    } catch (err) {
      sendResponse({
        ok: false,
        error: String((err && err.message) || err).slice(0, 200),
      });
      return false;
    }
    return false;
  });

  // If the page is unloaded (navigation/reload/bfcache evict), drop the overlay
  // so we never leave a stale cursor behind. The manifest re-injects content.js
  // on the next document, and the service worker re-establishes state.
  window.addEventListener("pagehide", removeOverlay, { once: true });
})();
