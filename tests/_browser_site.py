"""Local HTTP fixture site shared by the live browser test modules.

Not a test module (no test_* name, so pytest does not collect it): it serves
the small multi-page site the live suite drives a real Chromium against,
entirely on loopback.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DOWNLOAD_BODY = b"report-bytes-0123456789"
LOGIN_USER = "qauser"
LOGIN_PASS = "qa-pass-123"
CONSOLE_SECRET = "abcdef1234567890abcdef12"

#: A page that never stops drawing, like a game or 3D scene: every animation
#: frame bumps a counter, so a test can tell a parked page from a running one.
ANIMATED = """<!doctype html><html><head><title>Animated</title></head>
<body><p id="frames">0</p><canvas id="c" width="320" height="200"></canvas>
<script>
let n = 0; const g = document.getElementById("c").getContext("2d");
function tick(t) { n++; document.getElementById("frames").textContent = String(n);
  g.fillStyle = "hsl(" + (n % 360) + ",70%,50%)"; g.fillRect(0, 0, 320, 200);
  requestAnimationFrame(tick); }
requestAnimationFrame(tick);
</script></body></html>"""

#: A canvas page whose every animation frame blocks the main thread for
#: ~300 ms, like an always-rendering WebGL scene in a browser without a GPU.
HEAVY_CANVAS = """<!doctype html><html><head><title>Heavy canvas</title></head>
<body><canvas id="c" width="64" height="64"></canvas>
<button id="poke">Poke</button><p id="pokes">0</p>
<script>
let pokes = 0;
document.getElementById("poke").addEventListener("click", function () {
  pokes += 1; document.getElementById("pokes").textContent = String(pokes); });
function frame() { const until = performance.now() + 300;
  while (performance.now() < until) {} requestAnimationFrame(frame); }
requestAnimationFrame(frame);
</script></body></html>"""

#: Draws only on change, like an on-demand 3D viewer: busy frames while it
#: loads (unless sessionStorage says the page was "fixed") and for a few
#: frames after each poke; idle otherwise.
LAZY_CANVAS = """<!doctype html><html><head><title>Lazy canvas</title></head>
<body><canvas id="c" width="64" height="64"></canvas>
<button id="poke">Poke</button><p id="pokes">0</p>
<script>
let pokes = 0;
let dirty = sessionStorage.getItem("fixed") === "1" ? 0 : 1000;
document.getElementById("poke").addEventListener("click", function () {
  pokes += 1; document.getElementById("pokes").textContent = String(pokes); dirty = 6; });
function frame() {
  if (dirty > 0) { dirty -= 1; const until = performance.now() + 300; while (performance.now() < until) {} }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
</script></body></html>"""

INDEX = """<!doctype html><html><head><title>KaroX Browser Test Site</title></head>
<body>
<h1>Research Index</h1>
<p id="intro">Welcome to the local fixture site.</p>
<form id="login-form" method="post" action="/login">
  <label for="user">Username</label>
  <input id="user" name="username" type="text">
  <label for="mail">Email</label>
  <input id="mail" name="email" type="email">
  <label for="pw">Password</label>
  <input id="pw" name="password" type="password">
  <label for="plan">Plan</label>
  <select id="plan" name="plan"><option value="basic">Basic</option><option value="pro">Pro</option></select>
  <label for="agree">Agree</label>
  <input id="agree" name="agree" type="checkbox">
  <button id="login-btn" type="submit">Sign in</button>
</form>
<a id="page2-link" href="/page2">Second page</a>
<div id="tall" style="height:3000px">Spacer</div>
<div id="bottom-marker">Bottom of page</div>
<iframe id="embed" src="/frame" title="embed"></iframe>
<input id="file" type="file">
<span id="file-name"></span>
<a id="dl-link" href="/download" download>Download report</a>
<a id="api-link" href="/api/data">API data</a>
<button id="fetch-btn">Fetch JSON</button>
<button id="alert-btn">Open dialog</button>
<button id="add-item">Add item</button>
<ul id="items"><li>first item</li></ul>
<script>
  console.log("boot token=__TOKEN__ ready");
  document.getElementById('file').addEventListener('change', function (e) {
    document.getElementById('file-name').textContent = 'uploaded:' + e.target.files[0].name;
  });
  document.getElementById('fetch-btn').addEventListener('click', function () {
    fetch('/api/data').then(function (r) { return r.json(); }).then(function (d) {
      document.getElementById('intro').textContent = 'fetched:' + d.model;
    });
  });
  document.getElementById('alert-btn').addEventListener('click', function () {
    window.__alerted = (window.__alerted || 0) + 1;
    alert('consent?');
    window.__afterAlert = (window.__afterAlert || 0) + 1;
  });
  document.getElementById('add-item').addEventListener('click', function () {
    var btn = document.createElement('button');
    btn.textContent = 'added item ' + (document.getElementById('items').querySelectorAll('button').length + 1);
    document.getElementById('items').appendChild(btn);
  });
</script>
</body></html>
""".replace("__TOKEN__", CONSOLE_SECRET)

PAGE2 = """<!doctype html><html><head><title>Page Two</title></head>
<body><h1>Page Two</h1><a id="home-link" href="/">Back home</a></body></html>"""

FRAME = """<!doctype html><html><head><title>Frame</title></head>
<body><h2>Frame Content</h2><button id="frame-btn">Frame button</button></body></html>"""

REPLACE = """<!doctype html><html><head><title>Replace Lab</title></head>
<body><h1>Replace Lab</h1>
<div id="host"><button id="target">Hit me</button></div>
<button id="swap">Replace target</button>
<div id="out">hits:0</div>
<script>
  document.getElementById('target').addEventListener('click', function () {
    window.__hits = (window.__hits || 0) + 1;
    document.getElementById('out').textContent = 'hits:' + window.__hits;
  });
  document.getElementById('swap').addEventListener('click', function () {
    var host = document.getElementById('host');
    var old = document.getElementById('target');
    var fresh = old.cloneNode(true);
    host.innerHTML = '';
    host.appendChild(fresh);
    fresh.addEventListener('click', function () {
      window.__hits = (window.__hits || 0) + 1;
      document.getElementById('out').textContent = 'hits:' + window.__hits;
    });
  });
</script>
</body></html>"""

PRICING = """<!doctype html><html><head><title>Pricing</title></head>
<body><h1>Pricing</h1>
<section id="paid"><h2>Pro plan</h2><button id="buy-btn">Buy now for $19.99</button></section>
<section id="trial-ok"><h2>Trial</h2><p>Start your free trial - no card required.</p>
<button id="trial-btn">Start free trial</button></section>
<section id="trial-vague"><h2>Mystery</h2><button id="trial-vague-btn">Try free trial</button></section>
<script>
  for (const id of ['buy-btn','trial-btn','trial-vague-btn']) {
    document.getElementById(id).addEventListener('click', function () {
      window['__clicked_' + id] = 1;
    });
  }
</script>
</body></html>"""

WELCOME = """<!doctype html><html><head><title>Welcome</title></head>
<body><h1>Welcome back</h1><p id="who">user=__USER__</p><p id="plan-line">plan=__PLAN__</p>
</body></html>"""


class _SiteHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # silence test logs
        pass

    def _send(self, status: int, body: bytes, content_type: str, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        if route == "/":
            self._send(200, INDEX.encode("utf-8"), "text/html; charset=utf-8")
        elif route == "/page2":
            self._send(200, PAGE2.encode("utf-8"), "text/html; charset=utf-8")
        elif route == "/picture.svg":
            self._send(200, PICTURE_SVG.encode("utf-8"), "image/svg+xml")
        elif route == "/animated":
            self._send(200, ANIMATED.encode("utf-8"), "text/html; charset=utf-8")
        elif route == "/heavy-canvas":
            self._send(200, HEAVY_CANVAS.encode("utf-8"), "text/html; charset=utf-8")
        elif route == "/lazy-canvas":
            self._send(200, LAZY_CANVAS.encode("utf-8"), "text/html; charset=utf-8")
        elif route == "/frame":
            self._send(200, FRAME.encode("utf-8"), "text/html; charset=utf-8")
        elif route == "/replace":
            self._send(200, REPLACE.encode("utf-8"), "text/html; charset=utf-8")
        elif route == "/pricing":
            self._send(200, PRICING.encode("utf-8"), "text/html; charset=utf-8")
        elif route == "/welcome":
            cookie = self.headers.get("Cookie", "")
            user = "anonymous"
            plan = "none"
            for part in cookie.split(";"):
                if part.strip().startswith("who="):
                    user = part.strip()[4:]
                if part.strip().startswith("plan="):
                    plan = part.strip()[5:]
            body = WELCOME.replace("__USER__", user).replace("__PLAN__", plan)
            self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
        elif route == "/download":
            self._send(
                200,
                DOWNLOAD_BODY,
                "application/octet-stream",
                [("Content-Disposition", 'attachment; filename="report.bin"')],
            )
        elif route == "/api/data":
            payload = {
                "model": "fixture-model",
                "usage": {"input_tokens": 5, "output_tokens": 7},
                "secret_token": "leak-me-not",
                "authorization": "Bearer nope",
            }
            self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
        elif route == "/slow":
            time.sleep(2.5)
            self._send(200, b"slow done", "text/plain")
        else:
            self._send(404, b"missing", "text/plain")

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        if parsed.path == "/login":
            fields = parse_qs(raw.decode("utf-8"))
            user = (fields.get("username") or [""])[0]
            password = (fields.get("password") or [""])[0]
            plan = (fields.get("plan") or [""])[0]
            if user == LOGIN_USER and password == LOGIN_PASS:
                self._send(
                    302,
                    b"",
                    "text/plain",
                    [
                        ("Location", "/welcome"),
                        ("Set-Cookie", "session_token=fake-session-token-value; Path=/"),
                        ("Set-Cookie", f"who={user}; Path=/"),
                        ("Set-Cookie", f"plan={plan}; Path=/"),
                    ],
                )
            else:
                self._send(403, b"login refused", "text/plain")
        else:
            self._send(404, b"missing", "text/plain")


PICTURE_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 400 300' width='400' height='300'>"
    "<rect width='400' height='300' fill='#1e90ff'/><circle cx='200' cy='150' r='80' fill='#ffd700'/>"
    "</svg>"
)


@contextmanager
def site_server():
    """Serve the fixture site on a random loopback port for the test module."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), _SiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
