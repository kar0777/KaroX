# KaroX vNext — release readiness report

Branch: `codex/vnext-hybrid-runtime`
Base: `main` at `a5c233a`
Suite: `python -m unittest discover -s tests -p "test_*.py"` — **292 passed (2 skipped on Windows)**
Benchmark: `tests/test_benchmark.py` — **KB-HYBRID-01..10 all pass**

This report assesses release readiness for the vNext hybrid runtime as
implemented on this branch. It recommends an action and lists the blockers
that remain. It does not constitute a merge, push, or release.

## Recommendation

**Merge only after the new CI matrix is green.** The code-level release
blockers found in the hard review are addressed: proxy calls cross Core,
leases are cross-process serialized, verification commands are explicitly
approved, installed Skill/Pack bytes are integrity-bound, hosted built-in Core
tools have real MCP and OpenAPI wire E2E, and the installers expose the vNext
wheel through the primary `karox` command. A live-provider pass is still required
before any provider receives a live-tested label, but it is not a merge blocker
while those adapters remain labelled as fake-server conformance only.

The branch is ready for the recorded matrix, not for an unattended release.
No successful GitHub-hosted matrix run or paid-provider run is claimed here.

## KB-HYBRID benchmark summary

Run `python -m unittest discover -s tests -p "test_benchmark.py" -v` to
regenerate. Latency and usage vary by machine; reproducibility, not frozen
numbers, is the contract. A representative run on the development machine
(Windows, Python 3.13):

```text
ID            Pass   Lat(ms)  Usage                  Cost           Gate
------------------------------------------------------------------------
KB-HYBRID-01  OK       ~500  req=4,tok=8/4          -              failed check cannot report success
KB-HYBRID-02  OK      ~2400  -                      -              proxy double authorization boundary
KB-HYBRID-03  OK       ~570  req=5,tok=10/5         -              cross-provider mid-task model switch
KB-HYBRID-04  OK       ~560  req=5,tok=10/5         -              session recovery across restart
KB-HYBRID-05  OK      ~1790  req=2,tok=4/2          -              native agent uses external stdio MCP
KB-HYBRID-06  OK       ~0.1  -                      -              bridge credential secret isolation
KB-HYBRID-07  OK       ~85   -                      -              session lock blocks concurrent mutation
KB-HYBRID-08  OK      ~1680  -                      -              Notion transport regression
KB-HYBRID-09  OK       ~570  req=5,tok=10/5         -              structured handoff document
KB-HYBRID-10  OK       ~500  req=4,tok=16/8         0.4 USD        capstone verified run with routed cost
------------------------------------------------------------------------
10/10 gates passed
```

Each gate exercises the real runtime:

- **01** — a failing check cannot report success (durable verification chain
  resets on failure).
- **02** — proxy enforces both the hosted-client policy (`MCP_CALL`) and the
  session MCP selection; descriptors are secret-free.
- **03** — model A stops at the step limit on a leased session; model B
  resumes the same session and verifies without re-mutating.
- **04** — fresh `SessionStore`/`CoreRuntime` handles on the same on-disk
  paths recover the session and verify.
- **05** — the native agent routes a scripted tool-call to an MCP alias through
  Core to the real external stdio echo server (the path no earlier phase test
  exercised).
- **06** — bridge credentials live in the `KaroX/bridge` namespace; a
  provider-namespace reference cannot resolve; profile statuses are honest.
- **07** — a second lease on a held session is blocked by `SessionBusy`.
- **08** — the bundled Notion transport regression script exits 0.
- **09** — the structured handoff document is strict-JSON, content-digested,
  secret-free, and carries both models' history.
- **10** — a capstone verified run accumulates routed cost end-to-end
  (`0.40 USD`) in the session store and the agent report.

## Security review of the diff

The vNext diff adds a capability-gated Core, OS-keyring credential isolation,
an MCP proxy with a double authorization boundary, a strict Pack manifest,
and a secret-free structured handoff. Reviewed against the threat-driven test
list in `SECURITY.md`:

- No secret value is written to config, logs, headers, support bundles,
  session snapshots, evidence, or MCP results. Credentials are keyring
  references in dedicated namespaces; `redact()` is applied to session
  content, tool results, and handoff documents.
- The proxy never auto-exposes MCP; it forwards only allowlisted servers with
  allow-permitted tools as secret-free descriptors, and rejects non-hosted
  origins, empty allowlists, unselected servers, and schema drift. The real
  Streamable HTTP wire test also covers bearer denial and credential rotation.
- Built-in hosted tools are separately allowlisted with `--tool`, execute
  through Core under a `HOSTED_CLIENT` origin, and require a session lease plus
  stable idempotency key for mutations. The OpenAPI wire E2E covers auth,
  session binding, schema import, read/write, replay protection, and rotation.
- Session mutations require a lease + idempotency key; concurrent mutation is
  blocked; stale leases are fenced by a replacement token.
- The Pack SDK gates elevated permissions at install (explicit approval, no
  auto-grant) and confines every referenced file inside the pack directory;
  Pack-declared MCP remains metadata only this phase, so a Pack cannot bypass
  Core policy.
- A failed check cannot report success (KB-HYBRID-01), so a model cannot lie
  its way to a verified report.

No new secret-handling defect was found in the diff. The known baseline gaps
(`SECURITY.md` "Known baseline gaps") are legacy and are not introduced by this
diff.

## Blockers and pending human decisions

1. **Cross-platform matrix result (blocker for merge/release).**
   `.github/workflows/quality.yml` now builds the wheel, installs it, smoke-tests
   both `karox` and its `karox-vnext` compatibility alias, and runs all
   `tests/test_*.py` on Windows, macOS, and Linux.
   This remains open until that remote job actually passes.
2. **Live-service conformance (blocker for a "tested" label on providers).**
   Provider adapters pass deterministic in-process and real-fake-HTTP-server
   E2E. A live OpenAI/Anthropic/Gemini pass requires a user-supplied key and is
   not recorded on this branch. Needs: a paid account decision and key entry.
3. **Notion environment (environment-dependent).** KB-HYBRID-08 passes when
   the bundled Notion gateway server and its deps are importable; it fails
   honestly otherwise. Not a code blocker; document the requirement.
4. **PromptQL live product run (label blocker only).** The importable OpenAPI
   connector and Core wire path are tested locally. PromptQL remains
   `experimental` until a real workspace imports the schema and completes a
   recorded read/write/idempotency flow. Its public docs do not expose a remote
   web-agent invocation API, so CLI-to-PromptQL is not claimed.
5. **Context compaction (maturity work, not a blocker).** Durable history is
   still unbounded across many resumed runs. Handoff output is bounded to the
   latest 128 summarized records, but provider-request compaction is future work.
6. **Pack tool execution wiring (not a blocker this phase).** Pack-declared
   MCP is metadata only; tool registration for a Pack remains pending and is
   labelled as such in `IMPLEMENTATION_STATUS.md`.

## What was not done (honest)

- No merge, push, or release to `main`.
- No live provider E2E recorded.
- No successful macOS/Linux matrix result recorded yet; only the job is present.
- No README rewrite of the shipped product; the truthful implemented-only
  overview is `docs/vNext/README.md`.
- No frozen benchmark numbers committed; raw run records are regenerated by
  the suite.
