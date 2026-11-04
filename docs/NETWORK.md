# Network, TLS and provider transport

How KaroX reaches model providers, and the settings for networks that are not
a plain home connection.

## Proxies

Provider and catalog requests use httpx, which reads the standard variables:
`HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY` and `NO_PROXY` (lower-case spellings
too). A `socks5://` / `socks5h://` proxy needs the optional `socksio` package;
without it KaroX now says which variable points to SOCKS and how to add the
package, instead of an import error from deep inside the HTTP client:

```text
pipx inject karox-runtime6 socksio      # or: pip install "httpx[socks]"
```

`karox provider local` ignores proxy variables on purpose: its probes go to
`127.0.0.1`, which a proxy cannot reach.

## Certificates

| Variable | Effect |
| --- | --- |
| `KAROX_CA_BUNDLE=/path/ca.pem` | Trust this PEM bundle **in addition to** the public roots (certifi). For a TLS-inspecting corporate proxy, or a provider whose certificate is issued by a national CA such as the Russian Trusted Root CA (GigaChat). A missing file or a non-PEM file is reported by name. |
| `KAROX_TRUST_STORE=system` | Verify against the operating system's store (Windows certificate store, macOS keychain) through the optional `truststore` package — what pip itself does. Install it with `pipx inject karox-runtime6 truststore`. |
| `SSL_CERT_FILE=/path/ca.pem` | Standard OpenSSL/httpx variable: **replaces** the public roots with this bundle. |

`KAROX_CA_BUNDLE` wins over `KAROX_TRUST_STORE`. Any other value of
`KAROX_TRUST_STORE` than `system`, `certifi` or `default` is an error rather
than silently ignored.

## Timeouts

Each provider request has a connect (and connection-pool) timeout of at most
15 seconds, separate from the read/write budget of the request. An unreachable
host or a black-holed port therefore fails in seconds and goes to the retry
policy, instead of using up the whole model-call budget (often minutes) before
the first byte.

## Retries

Retried with backoff (up to six attempts per model call, about two minutes of
waiting at most): 429 rate limits, every 5xx including 529 "overloaded",
connection resets and timeouts, and a stream that stops mid-response. A
response whose body cannot be parsed is retried too, but at most twice, since
that can also be a systematic incompatibility. Authentication, permission,
budget and missing-model errors are not retried: they would fail the same way
again. A 400 is reported with the provider's message (an adapter that knows a
reduced form of the request may retry once with it).

The wait before a retry follows the provider when it says how long:

1. `Retry-After` (seconds or an HTTP date);
2. for a 429 without it: `retry-after-ms`, then OpenAI's
   `x-ratelimit-reset-requests` / `x-ratelimit-reset-tokens` (durations such
   as `1s`, `6m0s`, `250ms`) or Anthropic's `anthropic-ratelimit-*-reset` (an
   RFC 3339 time) — the reset of the exhausted limit when the remaining
   counts say which one it is, otherwise the longest.

Waits are capped by the remaining budget of the call. The fault stand in
`tests/test_provider_fault_stand.py` replays each of these failures against a
real loopback server in CI.

## Prompt caching

KaroX gives every request of one session the same cache identity:

* Anthropic Messages: a `cache_control` breakpoint on the system block (the
  tool table renders before it, so the whole static prefix is cached) and
  rolling breakpoints along the conversation, the last one on its tail;
* OpenAI (api.openai.com, Chat Completions and Responses; chatgpt.com
  Responses): `prompt_cache_key`, derived from the session and the digest of
  the system prompt and tools, so a different prompt or tool list never shares
  a key. Other OpenAI-compatible endpoints do not get the field (several
  reject unknown parameters);
* DeepSeek and other providers with automatic prefix caching need nothing.

Each assistant turn in the session record carries `prompt_cache`: how much
of the request prefix was unchanged since the previous request and, when a
cacheable prompt got no cached tokens at all (a full miss), why —
`first_request`, `tools_changed`, `history_rewrite`, `provider_miss`, or
`unreported` for a route that has never reported cached tokens. The session's usage counts
misses by cause (`prompt_cache_misses`), and `karox session export` shows both.
