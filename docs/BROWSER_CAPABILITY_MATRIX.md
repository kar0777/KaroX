# KaroX Browser capability matrix — v5 final pass, part 1

Audited 2026-08-21 against the shipping extension engine
(`chrome_extension_mv3`: `extension_browser.py` + `service_worker.js` +
`dom_helpers.js`), dispatched through the stable `browser.command` schema in
`workspace_worker.py`. Status vocabulary: WORKING / PARTIAL / MISSING, with
the closing change noted. The deterministic proof for every page-side verb is
`tests/test_browser_fixture_suite.py`, which drives the production
`dom_helpers.js` inside a real local browser over `tests/browser_fixtures/`.

## Navigation
| Capability | Status | Notes |
| --- | --- | --- |
| navigate (open / new_tab) | WORKING | pre-existing |
| back / forward | WORKING (new) | `back`/`forward` actions, settled-URL evidence |
| reload | WORKING (new surface) | worker `reload_page` now exposed as `reload` |
| current URL / title | WORKING (new) | `page_info` read; also in snapshot |
| wait selector/state | WORKING | `wait_for` attached/detached/hidden/visible |
| wait navigation/ready | PARTIAL | `open` waits; explicit ready-state wait folds into `wait_for` + `page_info.status` |

## Tabs
| Capability | Status |
| --- | --- |
| new / list / switch / close, stable identity | WORKING (pre-existing, `tabRef` identity) |

## Page understanding
| Capability | Status | Notes |
| --- | --- | --- |
| semantic snapshot, visible text, roles, labels, links | WORKING | pre-existing `snapshot` |
| control values/states, focus, disabled, geometry | WORKING (new) | `inspect` now reports focused / checked / value_length / rect / role |

## Locators
| Capability | Status | Notes |
| --- | --- | --- |
| css / text= / label= / role= | WORKING | role now filters by accname order (aria-label > label > placeholder) |
| placeholder= / testid= | WORKING (new) | `data-testid`, `data-test-id`, `data-test` |
| ambiguous target → candidates | WORKING (new) | fuzzy locators with >1 distinct visible match return `ambiguous_target` + candidate metadata; explicit `>> nth=K` disambiguates; no silent guess |

## Interaction
| Capability | Status | Notes |
| --- | --- | --- |
| click / fill / select / press | WORKING | pre-existing; press now carries modifier chords |
| dblclick / hover / focus / clear / type (per-key) | WORKING (new) | |
| checkbox / radio | WORKING (new) | deterministic `set_checked`, radio-uncheck refused |
| page scroll / container scroll / into view | WORKING (new) | `scroll` modes page / container / into_view |
| safe upload | WORKING (new) | agent-supplied base64 content only, never host paths; 10 files / 5 MB bound |
| download | WORKING (new, permission-gated) | browser download manager; typed `permission_denied` until the extension reload grants `downloads` |

## Visual
| Capability | Status | Notes |
| --- | --- | --- |
| viewport screenshot | WORKING | artifact-backed |
| element screenshot | WORKING (new) | crop via rect × devicePixelRatio, OffscreenCanvas |
| full-page screenshot | MISSING (extension engine) | captureVisibleTab is viewport-only; honest `viewport_only` flag; scroll-stitch deferred, not faked |

## Lifecycle & resources
| Capability | Status | Notes |
| --- | --- | --- |
| launch / reconnect / stop / recovery | WORKING | pre-existing, proven by `test_extension_browser.py`, `test_managed_browser.py` |
| no orphan browser/context/subprocess/pipe | WORKING | `test_resource_lifetime_regressions.py` + fixture suite closes browser and transport deterministically |

## Errors (typed, mandate 5)
`browser_errors.py` classifies every engine failure at the `_call` chokepoint:
`element_not_found`, `ambiguous_target`, `navigation_timeout`, `page_closed`,
`browser_disconnected`, `network_error`, `download_failure`,
`permission_denied`, `user_takeover_required`, fail-soft `browser_error`.
Nothing collapses into a generic browser failure.

## Evidence model (mandate 4)
Actions return compact typed evidence (`action`, `result`, counters, bounded
metadata); ambiguity returns candidates as data; screenshots return artifact
references; raw page state stays on demand (`snapshot`, `get_text`,
Session Detail). No unbounded DOM dumps ride along with actions.

## Safety (mandate 6, preserved)
User takeover, opaque local credential injection, secret non-disclosure,
payment/free-trial refusals, overlay isolation: unchanged and re-verified
(`test_extension_browser.py` 159 tests, takeover persistence suite). CAPTCHA /
2FA / OAuth consent / payment confirmations still require the physical user.
