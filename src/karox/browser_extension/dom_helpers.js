// dom_helpers.js — injected into ISOLATED world before snapshotPage/domAction.
// These functions are self-contained (no closure over service_worker globals)
// because chrome.scripting.executeScript does not transfer the service
// worker's closure. Using `var` makes them global in the ISOLATED world so
// snapshotPage and domAction can call them after a files-inject.
"use strict";

var OVERLAY_HOST_SELECTOR = "[data-karox-overlay-host]";

function visible(el) {
  if (!el) return false;
  var style = getComputedStyle(el);
  var rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity || 1) > 0 && rect.width >= 0 && rect.height >= 0;
}

function resolveSelector(selector) {
  var labelTarget = function (text) {
    var wanted = text.trim().toLowerCase();
    var labels = document.querySelectorAll("label");
    for (var i = 0; i < labels.length; i++) {
      var label = labels[i];
      var labelText = (label.innerText || label.textContent || "").trim().toLowerCase();
      if (labelText === wanted || labelText.indexOf(wanted) !== -1) {
        if (label.htmlFor) {
          var byId = document.getElementById(label.htmlFor);
          if (byId) return byId;
        }
        var nested = label.querySelector("input,textarea,select,button");
        if (nested) return nested;
      }
    }
    return null;
  };
  var textTarget = function (text) {
    var wanted = text.trim().toLowerCase();
    var candidates = document.querySelectorAll("button,a,input[type=submit],[role=button],[role=link],label");
    for (var i = 0; i < candidates.length; i++) {
      var el = candidates[i];
      var value = (el.innerText || el.value || el.getAttribute("aria-label") || "").trim().toLowerCase();
      if ((value === wanted || value.indexOf(wanted) !== -1) && visible(el)) return el;
    }
    return null;
  };
  var roleTarget = function (raw) {
    var match = raw.match(/^([^\[]+)(?:\[name=["']?(.*?)["']?\])?$/i);
    if (!match) return null;
    var role = match[1].trim();
    var name = (match[2] || "").trim().toLowerCase();
    var selector = '[role="' + CSS.escape(role) + '"]' + (role === "button" ? ",button" : ",__none__");
    var els = document.querySelectorAll(selector);
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var value = (el.innerText || el.value || el.getAttribute("aria-label") || "").trim().toLowerCase();
      if ((!name || value === name || value.indexOf(name) !== -1) && visible(el)) return el;
    }
    return null;
  };
  if (selector.startsWith("text=")) return textTarget(selector.slice(5));
  if (selector.startsWith("label=")) return labelTarget(selector.slice(6));
  if (selector.startsWith("role=")) return roleTarget(selector.slice(5));
  try { return document.querySelector(selector); } catch (e) { return null; }
}
