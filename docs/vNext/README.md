# Archived KaroX vNext development record

> **Historical documentation.** `vNext` was the development name for the hybrid
> runtime that became KaroX 5. Do not use this page as the current product quick
> start, release status, or compatibility claim.

Current canonical documents:

- [`../../README.md`](../../README.md) — KaroX 5 product overview;
- [`../V5_RELEASE_SCOPE.md`](../V5_RELEASE_SCOPE.md) — frozen 5.0 scope;
- [`../IMPLEMENTATION_STATUS.md`](../IMPLEMENTATION_STATUS.md) — current status;
- [`../CONNECTIVITY.md`](../CONNECTIVITY.md) — current connection paths;
- [`../RELEASE_CHECKLIST.md`](../RELEASE_CHECKLIST.md) — release execution record;
- [`../MIGRATION_V4_TO_V5.md`](../MIGRATION_V4_TO_V5.md) — migration contract;
- [`../conformance/README.md`](../conformance/README.md) — live evidence.

The remaining files in this directory preserve the architecture, security,
session, MCP, provider, Pack, roadmap, migration, and per-phase implementation
history written while the runtime lived on the `codex/vnext-hybrid-runtime`
branch. Their branch names and status statements are historical unless the same
fact is repeated in the canonical current documents above.

## Historical test-count evidence

For comparison with archived phase reports, the current unittest baseline contains 3506 cases. This is a discovery count, not a historical run result.

The unittest-plus-legacy subtotal is 3511: that baseline plus five legacy KaroX 4
checks under `scripts/`. It is not the full pytest count, which also includes
standalone functions and parameterized cases.

These current reference counts remain checked by `scripts/check_test_count.py`.
Individual archived phase documents retain their original historical counts.

## Historical document map

- `ARCHITECTURE.md` — original dependency and layering decisions;
- `SECURITY.md` — detailed threat model used during implementation;
- `SESSION-MODEL.md` — durable sessions, leases, idempotency, and handoff;
- `MCP-ARCHITECTURE.md` — MCP client, proxy, and hosted bridge design;
- `CONNECTIVITY.md` — original three-direction connectivity document;
- `PROVIDER-SDK.md` — provider adapter contract;
- `PACK-SDK.md` — Pack manifest and lifecycle design;
- `MIGRATION.md` — original dual-runtime migration strategy;
- `PRODUCT.md` — initial product framing;
- `ROADMAP.md` — implementation phases and gates;
- `IMPLEMENTATION_STATUS.md` — detailed phase-by-phase evidence and commit record;
- `RELEASE-READINESS.md` — historical branch readiness assessment;
- `TOOLING-NOTES.md` — implementation notes.

Corrections to historical facts should be made in the canonical current status
first. Preserve this directory when the old decision trail is useful, but do not
extend it with new shipping documentation.
