# External HTTPS browser for hosted MCP clients

KaroX keeps its original localhost browser mode and adds a separate, explicit
external-browser policy for ChatGPT Web, Claude Web, and other compatible MCP
clients. The external mode is intended for legal, user-directed inspection of
public HTTPS services: trial pages, signup forms, model pickers, usage, credits,
and billing metadata.

It is not an account-creation bot, CAPTCHA solver, checkout bot, or replacement
for user consent.

## Browser backends

KaroX exposes one MCP tool contract through two local backends:

- **KaroX Chrome extension** is the default for headed user takeover. It launches
  the installed Google Chrome with a dedicated persistent KaroX profile. The
  extension may control every ordinary tab in that profile, while the user's
  normal Chrome profile, cookies, history, and tabs remain outside its process.
  Agent navigation stays in background tabs; takeover alone focuses the work tab.
- **Playwright Chromium** remains available for headless/local verification and
  deterministic tests. It uses an isolated context and authenticated pinned-IP
  proxy and is not the preferred backend for Google or other identity providers.

Ordinary Google Chrome no longer accepts automatic `--load-extension` installation.
The first headed run therefore opens `chrome://extensions` and the generated KaroX
extension folder. Enable Developer mode and choose **Load unpacked** once. The
extension remains installed in the dedicated profile and reconnects to later KaroX
sessions through an authenticated loopback WebSocket.

## Access profile

Use `browser_control` when the hosted client needs browser read/input without
repository writes, process execution, or local commits. It grants:

- repository and local Git read;
- browser read and browser input;
- network access through the guarded browser policy.

It does **not** grant repository write, process execution, `git.commit`, Git push,
publishing, authentication commands, or payment confirmation.

The legacy profiles remain unchanged:

- `read_only` keeps browser input unavailable and external HTTPS disabled;
- `workspace_write` keeps its repository/check behavior;
- `elevated` remains the broadest local profile.

## Recommended ChatGPT saved profile

Allow only the domains needed for the current investigation. Omit
`--browser-payment-confirmation`; payment confirmation is intentionally absent by
default.

```powershell
karox bridge saved create chatgpt-browser `
  --target-profile chatgpt-web `
  --repository . `
  --tunnel tailscale `
  --browser-external-https `
  --browser-domain example.com `
  --browser-domain gitlab.com `
  --browser-headed `
  --browser-user-takeover `
  --browser-network-inspection `
  --language ru

karox bridge connect --saved chatgpt-browser
```

Add an email only when the agent is allowed to fill that exact address in the
current session:

```powershell
--browser-allowed-email your-address@example.com
```

Omitting all `--browser-domain` entries allows any public HTTPS domain for that
browser session, subject to the IP, redirect, scheme, download, takeover, and
payment guards below. A domain allowlist is safer and is recommended.

The running bridge process must be restarted or a new saved connection must be
started after upgrading KaroX so the child process publishes the new tool schema.
Do not restart or replace another agent's existing browser context or tunnel.

## Published MCP tools

All names are normal MCP tools and are not tied to one ChatGPT implementation.

| Tool | Input schema summary | Effect |
| --- | --- | --- |
| `karox.browser.open` | `url`, optional `width`, `height` | Opens an allowed URL in this session's context. |
| `karox.browser.tabs` | empty object | Lists only tabs owned by this KaroX session. |
| `karox.browser.new_tab` | optional `url` | Creates a new owned tab. |
| `karox.browser.switch_tab` | required `tab_id` | Activates an owned tab. |
| `karox.browser.close_tab` | required `tab_id` | Closes one owned tab, never another session or the last tab. |
| `karox.browser.snapshot` | empty object | Returns a redacted accessibility/DOM snapshot. |
| `karox.browser.screenshot` | optional `full_page`, `name` | Creates a session-owned PNG artifact. |
| `karox.browser.click` | required `selector` | Clicks after takeover/payment safety checks. |
| `karox.browser.fill` | required `selector`, `value` | Fills a non-sensitive allowed field. |
| `karox.browser.select` | required `selector`, `value` | Selects one or more values. |
| `karox.browser.press` | required `selector`, `key` | Presses a key; Enter/Space also run payment checks. |
| `karox.browser.wait_for` | optional `selector`, `state`, `milliseconds` | Waits for an element state or a bounded delay. |
| `karox.browser.get_text` | required `selector` | Returns bounded, redacted visible text. |
| `karox.browser.console` | optional `tab_id` | Returns redacted console entries. |
| `karox.browser.network_failures` | empty object | Returns redacted failed-request metadata. |
| `karox.browser.network_requests` | optional `url_contains`, `method`, `resource_type`, `fields` | Returns bounded safe network metadata. |
| `karox.browser.request_user_takeover` | optional `reason` | Pauses agent input and hands the visible window to the user. |
| `karox.browser.resume_after_user_takeover` | empty object | Resumes the same context and active tab. |
| `karox.browser.close` | empty object | Closes only this session's context, proxy, browser, and artifacts lifecycle. |

`network_requests` is published only with `--browser-network-inspection`.
Takeover tools are published only with `--browser-user-takeover`, which also
requires `--browser-headed`.

## URL and network threat model

Allowed:

- public `https://` destinations permitted by the session domain policy;
- `http://localhost`, `http://127.0.0.1`, and loopback IPv6 for deliberate local
  application verification;
- HTTPS localhost where the local application provides it.

Blocked:

- external plain HTTP;
- `file:`, `data:`, `javascript:`, `chrome:`, `chrome-extension:`, `ftp:`,
  `about:`, `blob:`, and unknown schemes;
- cloud metadata hosts and link-local metadata addresses;
- private, loopback, link-local, reserved, multicast, and unspecified addresses
  for external destinations;
- an external frame, redirect, or subrequest that attempts to reach localhost;
- a public hostname that resolves to any private or reserved address at explicit
  navigation validation time.

The Playwright backend additionally owns an authenticated HTTP CONNECT proxy on
a random loopback port. That proxy resolves the hostname, validates every
returned address, opens the upstream socket directly to the validated IP literal,
blocks service workers, and cancels automatic downloads.

The extension backend uses normal Chrome networking so identity providers see a
real browser profile. KaroX validates every agent-requested destination and blocks
unsafe schemes/private targets, but normal page subresources, service workers,
and user-initiated downloads follow Chrome's own policy. Diagnostics report this
as `navigation_validation_only`, never as pinned-proxy protection.

## Isolation

- headed extension mode owns one persistent Chrome profile under the KaroX runtime
  directory; the user's normal Chrome profile is never opened by KaroX;
- the extension may enumerate and control all ordinary tabs inside that dedicated
  profile, matching the user's explicit browser-wide permission;
- headless Playwright mode keeps one isolated context and one authenticated
  random-port proxy per KaroX session;
- screenshots and artifacts remain bound to the same KaroX session ID;
- the extension bridge accepts only its random loopback token and is never exposed
  through the public MCP tunnel;
- `browser.close` stops only the KaroX-owned Chrome/Playwright process and leaves
  the persistent extension profile available for the next run;
- neither backend changes Tailscale, Funnel, Serve, or MCP routes.

## User takeover

Typical flow:

1. The agent opens the registration or account page and fills only explicitly
   allowed non-sensitive fields.
2. At Google login, password entry, CAPTCHA, 2FA, consent, or ambiguous checkout,
   it calls `karox.browser.request_user_takeover`.
3. KaroX keeps the visible Chromium window and same browser context open but
   rejects parallel click/fill/select/press calls from the agent.
4. The user completes the sensitive step directly in that window.
5. After the user confirms completion, the agent calls
   `karox.browser.resume_after_user_takeover` and continues in the same active
   tab and authenticated context.

KaroX does not read back the user's password, 2FA code, CAPTCHA answer, cookies,
storage, authorization headers, or session tokens.

## Registration and payment boundaries

- An email can be filled only when its exact lower-cased value was allowed with
  `--browser-allowed-email` for this session.
- Password, credential, token, cookie, card, CVV/CVC, and similar fields are
  blocked and require user takeover.
- Unknown or uninspectable DOM targets fail closed and require takeover.
- A payment, purchase, checkout, upgrade, subscription, renewal, or credit-buying
  action is blocked without the separate `browser_payment_confirmation` policy.
- A free-trial control without that capability is clickable only when its nearest
  form/section/dialog clearly states zero price or no-card-required evidence.
  Ambiguous trial screens require user takeover.
- KaroX does not create multiple accounts, use temporary email, invent personal
  data, bypass CAPTCHA, change region, use fake payment methods, or accept a
  non-zero checkout automatically.

## Safe network inspection

Example:

```text
karox.browser.network_requests(
    url_contains="chat",
    fields=["model", "model_id", "provider", "usage", "credits"]
)
```

Returned metadata may include:

- redacted URL, method, status, content type, resource type, and timing;
- safe JSON field names;
- bounded values for `model`, `model_id`, `provider`, `usage`, `credits`, `plan`,
  `trial`, and `subscription`;
- safe nested metadata such as token totals, balance, remaining credits, price,
  currency, status, billing interval, and expiry/renewal timestamps;
- request failures.

Never returned:

- request or response authorization headers;
- cookies or browser storage;
- access/refresh tokens, CSRF/XSRF values, API keys, credentials, or passwords;
- full session IDs or high-entropy path/query values;
- card/PAN/CVV/CVC/IBAN data;
- arbitrary personal messages, uploaded files, or unbounded JSON bodies.

Query values and fragments are removed. High-entropy path segments are replaced.
A JSON response body is inspected only when it declares a positive content length
at or below 1 MiB; otherwise KaroX returns metadata only.

## Diagnostics example

An external ChatGPT browser profile reports fields of this form:

```json
{
  "access_profile": "browser_control",
  "write_permission": false,
  "browser_permission": {
    "read": true,
    "input": true,
    "localhost": true,
    "external_https": true,
    "user_takeover": true,
    "network_inspection": true,
    "payment_confirmation": false,
    "headed": true,
    "backend": "extension",
    "localhost_only": false
  },
  "browser_isolation": {
    "context_per_session": true,
    "cross_session_control": false,
    "artifacts_bound_to_session": true,
    "dedicated_chrome_profile": true,
    "main_chrome_profile_visible": false,
    "dns_pinning_proxy": false,
    "proxy_authentication": "not_used",
    "proxy_port": "not_used"
  },
  "url_policy": {
    "https_external": "allowed",
    "localhost": "allowed",
    "external_http": "blocked",
    "private_network": "blocked",
    "metadata_endpoints": "blocked",
    "unsafe_schemes": "blocked",
    "redirects_revalidated": true,
    "external_to_localhost": "blocked",
    "dns_rebinding": "navigation_validation_only",
    "service_workers": "browser_default",
    "downloads": "browser_default"
  }
}
```

`disabled_tools` names tools that are not selected and explains when network
inspection or takeover was not enabled.

## Verified smoke test

The Windows smoke test `tests/manual_external_browser_example_smoke.py` performs
no login and submits no form. Its latest local run verified:

- `https://example.com/` opened with title `Example Domain` through the authenticated
  pinned-IP proxy;
- a DOM snapshot and session-owned PNG were created;
- a second tab was opened, switched away from, and closed, leaving one owned tab;
- `https://gitlab.com/users/sign_up` was opened only to its first form;
- 12 signup inputs were observed, with no fill/click/submit;
- 33 redacted GitLab network records were inspected;
- diagnostics reported external HTTPS, network inspection, and context-per-session;
- the legacy localhost validator still accepted `http://127.0.0.1:8080/`.

A deterministic test suite covers URL policy, private-address redirects, DNS
pinning, proxy authentication, cross-session tab isolation, takeover/resume,
payment blocking, network redaction, tool publication, CLI flags, saved profiles,
and old localhost behavior.

## Remaining limitations

- The Chrome extension must be loaded once through `chrome://extensions` in the
  dedicated KaroX profile; ordinary Chrome removed automatic unpacked-extension
  loading from the command line.
- Headed takeover requires a graphical desktop session and installed Google
  Chrome. Headless verification still requires Playwright and its Chromium binary.
- Extension screenshots are viewport captures. KaroX may briefly switch the tab
  inside its separate Chrome window and restores the previously visible tab.
- Extension mode reports safe request metadata but does not inspect arbitrary
  response bodies or page console output.
- Sites may present anti-bot pages, deny scripted DOM actions, or change labels;
  KaroX does not bypass those controls and hands sensitive steps to the user.
- The dedicated profile/context boundary is application-level isolation, not an
  operating-system or VM sandbox.
- A real ChatGPT account conformance run is still separate evidence from the
  local MCP contract and browser smoke test.
