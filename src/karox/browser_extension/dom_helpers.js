// dom_helpers.js — injected into the ISOLATED world before snapshotPage and
// domAction run. These functions are self-contained (no closure over
// service_worker globals) because chrome.scripting.executeScript does not
// transfer the service worker's closure. Using `var` makes them global in the
// ISOLATED world so injected functions can call them after a files-inject.
//
// This file is also the single source of truth for the page-side action
// engine: the deterministic browser fixture suite loads it into a plain page
// and drives domAction directly, so the code the tests verify is the code
// production injects.
"use strict";

var OVERLAY_HOST_SELECTOR = "[data-karox-overlay-host]";

var KAROX_SECRET_HINT = /password|secret|token|api[_-]?key|credential|cookie|card|cvv|cvc|iban/i;

function visible(el) {
  if (!el) return false;
  var style = getComputedStyle(el);
  var rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity || 1) > 0 && rect.width >= 0 && rect.height >= 0;
}

function karoxIsSensitive(el) {
  var fieldType = String(el.getAttribute && el.getAttribute("type") || "").toLowerCase();
  var marked = (el.getAttribute && el.getAttribute("data-karox-secret")) === "true" || (el.dataset && el.dataset.karoxSecret === "true");
  return marked || fieldType === "password" || KAROX_SECRET_HINT.test([
    (el.getAttribute && el.getAttribute("name")) || "",
    el.id || "",
    (el.getAttribute && el.getAttribute("aria-label")) || "",
    (el.getAttribute && el.getAttribute("placeholder")) || "",
  ].join(" "));
}

// Safe, bounded metadata for one element: enough to tell candidates apart,
// never a secret value and never unbounded page text.
function karoxCandidateMeta(el) {
  var sensitive = karoxIsSensitive(el);
  return {
    tag: String(el.tagName || "").toLowerCase(),
    type: String((el.getAttribute && el.getAttribute("type")) || "").toLowerCase(),
    name: String((el.getAttribute && el.getAttribute("name")) || ""),
    id: String(el.id || ""),
    role: String((el.getAttribute && el.getAttribute("role")) || ""),
    aria: String((el.getAttribute && el.getAttribute("aria-label")) || ""),
    placeholder: String((el.getAttribute && el.getAttribute("placeholder")) || ""),
    testid: String((el.getAttribute && (el.getAttribute("data-testid") || el.getAttribute("data-test-id") || el.getAttribute("data-test"))) || ""),
    text: sensitive ? "" : String(el.innerText || el.value || "").trim().slice(0, 80),
    visible: visible(el),
    disabled: Boolean(el.disabled),
  };
}

// The accessible name of a control, best-effort and cheap: explicit
// aria-label first, then any associated <label>, then the placeholder, then
// the element's own text or value. This is what role= filters against.
function karoxAccessibleText(el) {
  var aria = (el.getAttribute && el.getAttribute("aria-label")) || "";
  if (aria.trim()) return aria;
  if (el.labels && el.labels.length) {
    var joined = "";
    for (var i = 0; i < el.labels.length; i++) {
      joined += " " + (el.labels[i].innerText || el.labels[i].textContent || "");
    }
    if (joined.trim()) return joined;
  }
  var placeholder = (el.getAttribute && el.getAttribute("placeholder")) || "";
  if (placeholder.trim()) return placeholder;
  return el.innerText || el.value || "";
}

// One locator grammar for every action. Strategies:
//   css (default)        querySelectorAll document order
//   text=NEEDLE          buttons/links/labels whose accessible text matches
//   label=NEEDLE         form control reached through its <label>
//   role=ROLE[name="X"]  explicit or implicit ARIA role, filtered by name
//   placeholder=NEEDLE   input/textarea by placeholder text
//   testid=VALUE         [data-testid] / [data-test-id] / [data-test]
// A trailing " >> nth=K" picks match K (0-based) explicitly.
function karoxParseLocator(raw) {
  var selector = String(raw || "");
  var nth = null;
  var nthMatch = selector.match(/\s*>>\s*nth=(\d+)\s*$/);
  if (nthMatch) {
    nth = Number(nthMatch[1]);
    selector = selector.slice(0, nthMatch.index);
  }
  var strategies = ["text=", "label=", "role=", "placeholder=", "testid="];
  for (var i = 0; i < strategies.length; i++) {
    if (selector.indexOf(strategies[i]) === 0) {
      return { strategy: strategies[i].slice(0, -1), value: selector.slice(strategies[i].length), nth: nth };
    }
  }
  return { strategy: "css", value: selector, nth: nth };
}

function karoxMatchesFor(strategy, value) {
  var wanted = String(value || "").trim().toLowerCase();
  var out = [];
  var els;
  var i;
  var el;
  var textOf;
  if (strategy === "css") {
    try { els = document.querySelectorAll(value); } catch (e) { return []; }
    for (i = 0; i < els.length; i++) out.push(els[i]);
    return out;
  }
  if (strategy === "testid") {
    els = document.querySelectorAll("[data-testid],[data-test-id],[data-test]");
    for (i = 0; i < els.length; i++) {
      el = els[i];
      var tid = el.getAttribute("data-testid") || el.getAttribute("data-test-id") || el.getAttribute("data-test") || "";
      if (tid === value) out.push(el);
    }
    return out;
  }
  if (strategy === "placeholder") {
    els = document.querySelectorAll("input[placeholder],textarea[placeholder]");
    for (i = 0; i < els.length; i++) {
      el = els[i];
      var ph = String(el.getAttribute("placeholder") || "").trim().toLowerCase();
      if (ph === wanted || ph.indexOf(wanted) !== -1) out.push(el);
    }
    return out;
  }
  if (strategy === "label") {
    var labels = document.querySelectorAll("label");
    for (i = 0; i < labels.length; i++) {
      var label = labels[i];
      var labelText = (label.innerText || label.textContent || "").trim().toLowerCase();
      if (labelText === wanted || labelText.indexOf(wanted) !== -1) {
        var target = null;
        if (label.htmlFor) target = document.getElementById(label.htmlFor);
        if (!target) target = label.querySelector("input,textarea,select,button");
        if (target && out.indexOf(target) === -1) out.push(target);
      }
    }
    return out;
  }
  if (strategy === "text") {
    els = document.querySelectorAll("button,a,input[type=submit],[role=button],[role=link],label,summary");
    for (i = 0; i < els.length; i++) {
      el = els[i];
      textOf = (el.innerText || el.value || el.getAttribute("aria-label") || "").trim().toLowerCase();
      if ((textOf === wanted || textOf.indexOf(wanted) !== -1) && visible(el)) out.push(el);
    }
    return out;
  }
  if (strategy === "role") {
    var match = String(value).match(/^([^\[]+)(?:\[name=["']?(.*?)["']?\])?$/i);
    if (!match) return [];
    var role = match[1].trim().toLowerCase();
    var name = (match[2] || "").trim().toLowerCase();
    var implicit = { button: "button,input[type=button],input[type=submit]", link: "a[href]", checkbox: "input[type=checkbox]", radio: "input[type=radio]", textbox: "input:not([type]),input[type=text],input[type=email],input[type=search],textarea", combobox: "select" };
    var css = '[role="' + role.replace(/"/g, "") + '"]' + (implicit[role] ? "," + implicit[role] : "");
    try { els = document.querySelectorAll(css); } catch (e) { return []; }
    for (i = 0; i < els.length; i++) {
      el = els[i];
      textOf = String(karoxAccessibleText(el)).trim().toLowerCase().replace(/\s+/g, " ");
      if ((!name || textOf === name || textOf.indexOf(name) !== -1) && visible(el)) out.push(el);
    }
    return out;
  }
  return [];
}

// Exact accessible-text matches outrank substring matches, so "Save" prefers
// the Save button over "Save as draft" instead of tripping ambiguity.
function karoxRankMatches(strategy, value, matches) {
  if (strategy === "css" || strategy === "testid") return matches;
  var wanted = String(value || "").trim().toLowerCase();
  var exact = [];
  var loose = [];
  for (var i = 0; i < matches.length; i++) {
    var el = matches[i];
    var textOf = "";
    if (strategy === "placeholder") textOf = String(el.getAttribute("placeholder") || "").trim().toLowerCase();
    else if (strategy === "role") textOf = String(karoxAccessibleText(el)).trim().toLowerCase().replace(/\s+/g, " ");
    else textOf = (el.innerText || el.value || el.getAttribute("aria-label") || el.textContent || "").trim().toLowerCase().replace(/\s+/g, " ");
    if (strategy === "role") {
      var m = String(value).match(/\[name=["']?(.*?)["']?\]/i);
      wanted = (m ? m[1] : "").trim().toLowerCase();
      if (!wanted) { loose.push(el); continue; }
    }
    if (textOf === wanted) exact.push(el); else loose.push(el);
  }
  return exact.length ? exact : matches;
}

function resolveSelectorAll(selector) {
  var parsed = karoxParseLocator(selector);
  var matches = karoxMatchesFor(parsed.strategy, parsed.value);
  return karoxRankMatches(parsed.strategy, parsed.value, matches);
}

// Back-compatible single resolution: the first match, or null.
function resolveSelector(selector) {
  var parsed = karoxParseLocator(selector);
  var matches = karoxRankMatches(parsed.strategy, parsed.value, karoxMatchesFor(parsed.strategy, parsed.value));
  if (!matches.length) return null;
  if (parsed.nth !== null) return matches[parsed.nth] || null;
  return matches[0];
}

// The strict resolution every action uses. Fuzzy strategies with several
// distinct visible matches and no explicit nth= return the candidates rather
// than silently acting on the first: never guess where the click lands.
function karoxResolveTarget(selector) {
  var parsed = karoxParseLocator(selector);
  var matches = karoxRankMatches(parsed.strategy, parsed.value, karoxMatchesFor(parsed.strategy, parsed.value));
  if (!matches.length) return { error_kind: "element_not_found", selector: String(selector).slice(0, 300) };
  if (parsed.nth !== null) {
    var picked = matches[parsed.nth];
    if (!picked) return { error_kind: "element_not_found", selector: String(selector).slice(0, 300) };
    return { element: picked };
  }
  if (parsed.strategy !== "css" && matches.length > 1) {
    var distinct = [];
    for (var i = 0; i < matches.length && distinct.length < 5; i++) {
      if (distinct.indexOf(matches[i]) === -1) distinct.push(matches[i]);
    }
    if (distinct.length > 1) {
      var candidates = [];
      for (var j = 0; j < distinct.length; j++) candidates.push(karoxCandidateMeta(distinct[j]));
      return { error_kind: "ambiguous_target", selector: String(selector).slice(0, 300), candidates: candidates };
    }
  }
  return { element: matches[0] };
}

function karoxKeyEventInit(spec) {
  var parts = String(spec || "").split("+");
  var key = parts.pop() || "";
  var init = { key: key, bubbles: true, cancelable: true };
  for (var i = 0; i < parts.length; i++) {
    var mod = parts[i].trim().toLowerCase();
    if (mod === "control" || mod === "ctrl") init.ctrlKey = true;
    else if (mod === "shift") init.shiftKey = true;
    else if (mod === "alt") init.altKey = true;
    else if (mod === "meta" || mod === "cmd" || mod === "command") init.metaKey = true;
  }
  return init;
}

function karoxSetNativeValue(el, value) {
  var proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  var setter = Object.getOwnPropertyDescriptor(proto, "value") && Object.getOwnPropertyDescriptor(proto, "value").set;
  if (setter) setter.call(el, value); else el.value = value;
}

function karoxPointerHover(el) {
  var rect = el.getBoundingClientRect();
  var x = rect.left + rect.width / 2;
  var y = rect.top + rect.height / 2;
  var names = ["pointerover", "pointerenter", "mouseover", "mouseenter", "mousemove"];
  for (var i = 0; i < names.length; i++) {
    var type = names[i];
    var EventCtor = type.indexOf("pointer") === 0 ? PointerEvent : MouseEvent;
    el.dispatchEvent(new EventCtor(type, { bubbles: type !== "mouseenter" && type !== "pointerenter", clientX: x, clientY: y }));
  }
}

// The page-side action engine. Returns compact typed evidence; the caller
// (service worker, then the Python bridge) never receives unbounded page
// text from an action result. Ambiguity and absence come back as typed
// error_kind results so no layer has to parse prose.
function domAction(payload) {
  var action = String(payload.action || "");
  // Page-level scrolling has no target element to resolve.
  if (action === "scroll" && (payload.mode === "page" || (!payload.selector && !payload.mode))) {
    if (payload.to === "top") window.scrollTo({ top: 0 });
    else if (payload.to === "bottom") window.scrollTo({ top: document.documentElement.scrollHeight });
    else window.scrollBy({ left: Number(payload.dx || 0), top: Number(payload.dy || 0) });
    return { action: "scroll", result: "success", mode: "page", x: Math.round(window.scrollX), y: Math.round(window.scrollY) };
  }
  var resolved = karoxResolveTarget(String(payload.selector || ""));
  if (resolved.error_kind) return resolved;
  var el = resolved.element;
  if (el.closest && el.closest(OVERLAY_HOST_SELECTOR)) {
    throw new Error("element is part of the KaroX overlay and cannot be acted on");
  }
  var scope = el.closest ? el.closest("form,section,article,[role=dialog]") : null;
  var fieldType = String(el.getAttribute && el.getAttribute("type") || el.tagName || "").toLowerCase();
  var sensitive = karoxIsSensitive(el);
  var rect = el.getBoundingClientRect();
  var metadata = {
    type: fieldType,
    name: String((el.getAttribute && el.getAttribute("name")) || ""),
    id: String(el.id || ""),
    aria: String((el.getAttribute && el.getAttribute("aria-label")) || ""),
    placeholder: String((el.getAttribute && el.getAttribute("placeholder")) || ""),
    role: String((el.getAttribute && el.getAttribute("role")) || ""),
    secret: sensitive,
    text: sensitive ? "" : String(el.innerText || el.value || "").trim().slice(0, 500),
    context: String((scope && scope.innerText) || "").trim().slice(0, 2200),
    disabled: Boolean(el.disabled),
    visible: visible(el),
    focused: document.activeElement === el,
    checked: typeof el.checked === "boolean" ? el.checked : null,
    value_length: typeof el.value === "string" ? el.value.length : null,
    rect: { x: Math.round(rect.left), y: Math.round(rect.top), width: Math.round(rect.width), height: Math.round(rect.height) },
  };
  if (action === "inspect") return metadata;
  if (action === "click" || action === "dblclick") {
    el.scrollIntoView({ block: "center", inline: "center" });
    el.focus({ preventScroll: true });
    if (action === "dblclick") {
      el.dispatchEvent(new MouseEvent("dblclick", { bubbles: true, cancelable: true }));
      el.click();
      return { action: "dblclick", result: "success", clicked: true, metadata: metadata };
    }
    el.click();
    return { action: "click", result: "success", clicked: true, metadata: metadata };
  }
  if (action === "hover") {
    el.scrollIntoView({ block: "center", inline: "center" });
    karoxPointerHover(el);
    return { action: "hover", result: "success", hovered: true, metadata: metadata };
  }
  if (action === "focus") {
    el.focus({ preventScroll: false });
    return { action: "focus", result: "success", focused: document.activeElement === el, metadata: metadata };
  }
  if (action === "clear") {
    el.focus({ preventScroll: true });
    karoxSetNativeValue(el, "");
    el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "deleteContentBackward" }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { action: "clear", result: "success", cleared: true, value_length: 0 };
  }
  if (action === "fill" || action === "fill_secret") {
    var secretFill = action === "fill_secret";
    if (secretFill) {
      el.dataset.karoxSecret = "true";
      el.setAttribute("data-karox-secret", "true");
      el.setAttribute("autocomplete", "off");
      el.style.setProperty("-webkit-text-security", "disc", "important");
      el.style.setProperty("text-security", "disc", "important");
    }
    el.focus({ preventScroll: true });
    var value = String(payload.value == null ? "" : payload.value);
    karoxSetNativeValue(el, value);
    if (secretFill) {
      el.dispatchEvent(new Event("input", { bubbles: true }));
    } else {
      el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
    }
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return secretFill
      ? { action: action, result: "success", filled: true, secret: true }
      : { action: action, result: "success", filled: true, value_length: value.length, metadata: metadata };
  }
  if (action === "type") {
    el.focus({ preventScroll: true });
    var text = String(payload.value == null ? "" : payload.value).slice(0, 20000);
    var current = typeof el.value === "string" ? el.value : "";
    for (var t = 0; t < text.length; t++) {
      var ch = text[t];
      el.dispatchEvent(new KeyboardEvent("keydown", { key: ch, bubbles: true }));
      current += ch;
      karoxSetNativeValue(el, current);
      el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: ch }));
      el.dispatchEvent(new KeyboardEvent("keyup", { key: ch, bubbles: true }));
    }
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { action: "type", result: "success", typed: true, value_length: text.length };
  }
  if (action === "select") {
    var values = Array.isArray(payload.value) ? payload.value.map(String) : [String(payload.value)];
    for (var o = 0; o < (el.options || []).length; o++) {
      var option = el.options[o];
      option.selected = values.indexOf(option.value) !== -1;
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { action: "select", result: "success", selected: true, count: values.length, metadata: metadata };
  }
  if (action === "set_checked") {
    var wantChecked = payload.checked !== false;
    if (typeof el.checked !== "boolean") throw new Error("element is not a checkbox or radio");
    if (fieldType === "radio" && !wantChecked) throw new Error("a radio button cannot be unchecked directly; check another option");
    if (el.checked !== wantChecked) {
      el.click();
      if (el.checked !== wantChecked) {
        el.checked = wantChecked;
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }
    return { action: "set_checked", result: "success", checked: Boolean(el.checked) };
  }
  if (action === "press") {
    var init = karoxKeyEventInit(payload.key);
    el.focus({ preventScroll: true });
    el.dispatchEvent(new KeyboardEvent("keydown", init));
    el.dispatchEvent(new KeyboardEvent("keyup", init));
    var plain = !init.ctrlKey && !init.shiftKey && !init.altKey && !init.metaKey;
    if (plain && String(init.key).toLowerCase() === "enter") {
      var form = el.closest && el.closest("form");
      if (form && form.requestSubmit) form.requestSubmit();
    } else if (plain && (init.key === " " || String(init.key).toLowerCase() === "space")) {
      el.click();
    }
    return { action: "press", result: "success", pressed: true, key: String(payload.key || ""), metadata: metadata };
  }
  if (action === "scroll") {
    if (payload.mode === "into_view") {
      el.scrollIntoView({ block: "center", inline: "center" });
      return { action: "scroll", result: "success", mode: "into_view" };
    }
    el.scrollBy({ left: Number(payload.dx || 0), top: Number(payload.dy || 0) });
    return { action: "scroll", result: "success", mode: "container", x: Math.round(el.scrollLeft), y: Math.round(el.scrollTop) };
  }
  if (action === "upload") {
    if (!(el instanceof HTMLInputElement) || String(el.type).toLowerCase() !== "file") {
      throw new Error("upload target must be an input[type=file]");
    }
    var files = Array.isArray(payload.files) ? payload.files : [];
    if (!files.length) throw new Error("upload requires at least one file");
    var total = 0;
    var transfer = new DataTransfer();
    for (var f = 0; f < files.length && f < 10; f++) {
      var spec = files[f] || {};
      var binary = atob(String(spec.content_base64 || ""));
      total += binary.length;
      if (total > 5 * 1024 * 1024) throw new Error("upload refuses more than 5 MB of file content");
      var bytes = new Uint8Array(binary.length);
      for (var b = 0; b < binary.length; b++) bytes[b] = binary.charCodeAt(b);
      transfer.items.add(new File([bytes], String(spec.name || "upload.bin"), { type: String(spec.mime || "application/octet-stream") }));
    }
    el.files = transfer.files;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { action: "upload", result: "success", uploaded: true, count: transfer.files.length };
  }
  if (action === "text") {
    if (sensitive) return { action: "text", result: "success", text: "", secret: true };
    return { action: "text", result: "success", text: String(el.innerText || el.textContent || el.value || "").slice(0, 30000), metadata: metadata };
  }
  throw new Error("unsupported DOM action");
}
