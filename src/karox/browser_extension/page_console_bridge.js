"use strict";
(() => {
  const NS = "__karox_console_bridge__";
  if (window.__karoxConsoleBridge) return;
  window.__karoxConsoleBridge = true;

  const MAX_LEN = 1200;
  const MAX_DEPTH = 4;
  const MAX_ARRAY = 20;
  const MAX_KEYS = 30;
  // Best-effort secret scrub. The KaroX bridge applies a second redaction pass,
  // but we never forward DOM nodes, cookies, storage or raw credential-shaped
  // values from the page's MAIN world.
  const SECRET_KEY = /password|passwd|secret|token|cookie|auth|authorization|api[_-]?key|session|credential|card|cvv|cvc|pan|iban|private[_-]?key|refresh|access[_-]?token/i;

  const orig = {
    debug: console.debug,
    log: console.log,
    info: console.info,
    warn: console.warn,
    error: console.error,
  };

  function clip(value, max) {
    const s = String(value);
    return s.length > max ? s.slice(0, max) + "…" : s;
  }

  function safe(value, depth) {
    if (depth > MAX_DEPTH) return "[deep]";
    if (value === null || value === undefined) return value;
    const t = typeof value;
    if (t === "function") return "[function]";
    if (t === "bigint") return String(value);
    if (t === "symbol") return String(value);
    if (t !== "object") return clip(value, MAX_LEN);
    // DOM nodes, Window, and other host objects never cross the boundary.
    if (value instanceof Node) return "[node:" + (value.nodeName || "?").toLowerCase() + "]";
    if (value instanceof Error) {
      return {
        name: clip(value.name || "Error", 80),
        message: clip(value.message || "", 300),
        stack: clip(value.stack || "", 300),
      };
    }
    if (value instanceof RegExp || value instanceof Date) return String(value);
    if (Array.isArray(value)) {
      return value.slice(0, MAX_ARRAY).map((x) => safe(x, depth + 1));
    }
    try {
      const out = {};
      let n = 0;
      for (const key of Object.keys(value)) {
        if (n++ >= MAX_KEYS) {
          out["…"] = true;
          break;
        }
        if (SECRET_KEY.test(key)) {
          out[key] = "[redacted]";
          continue;
        }
        out[key] = safe(value[key], depth + 1);
      }
      return out;
    } catch {
      return "[unserializable]";
    }
  }

  function wrap(level) {
    return function (...args) {
      // The page console must keep working even if our relay throws or the
      // receiving content script is gone; never let an error here surface.
      try {
        const payload = {
          ns: NS,
          type: "console",
          level: level,
          text: args.map((a) => safe(a, 0)).join(" "),
          at: Date.now(),
        };
        window.postMessage(payload, "*");
      } catch {
        /* best-effort; page console still calls the original below */
      }
      orig[level].apply(console, args);
    };
  }

  for (const lvl of ["debug", "log", "info", "warn", "error"]) {
    console[lvl] = wrap(lvl);
  }
})();
