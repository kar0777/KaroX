# KaroX 5 — plan for CLI completion, Skills, MCP and the free Marketplace

Status: **proposed, not yet approved scope**
Runtime version at planning time: `5.0.0.dev0`

This plan covers the work requested after the test-suite hang was fixed: finish
what the CLI exposes, add real development Skills (computer use, frontend
design), extend MCP, and add a free Marketplace that installs Skills published
in ordinary Git repositories.

## 1. What already exists (verified, not assumed)

Read from the repository at planning time:

- `karox pack` already implements a full install system: strict
  `karox-pack.toml` manifest, per-file content hashing, atomic staging,
  cross-process file locking, platform and KaroX-version compatibility checks,
  and install-time permission approval with no auto-grant.
- `karox skill list/show/load/select/deselect` exists. `SkillCatalog` discovers
  from `.karox/skills`, `.agents/skills`, `.claude/skills`, explicit
  `--skill-dir`, and a global directory, with bounded reads and strict YAML.
- `karox mcp server/session/status/call/credential` exists, plus
  `src/karox/agent_mcp.py` exposing KaroX itself as an MCP server.
- `Capability` already defines `browser.read`, `browser.input`, `desktop.input`,
  `network`, `mcp.call`. The policy layer already treats them as elevated.
- Browser automation exists in `remote_tools.py` via Playwright, but is
  restricted to localhost URLs and is only reachable on the remote path.
- Exactly one Skill ships: `skills/karox-remote`. There is no computer-use or
  frontend-design Skill.

So this is not a green field. The correct engineering move is to **extend the
Pack/Skill machinery that is already hardened**, not to build a second parallel
installer.

## 2. The real release blocker

`scripts/check_v5_release.py --strict` currently fails, and not because of code
quality. It fails because the release contract requires *live* evidence:

- six integrations (`chatgpt-web`, `claude-web`, `openai-responses`,
  `anthropic-messages`, `gemini`, `openai-compatible`) are all `pending`;
- external beta is `pending` and requires >= 5 unaided installations,
  >= 3 completed primary scenarios, >= 2 completed without maintainer help,
  >= 2 second-task users;
- one-time fix scripts must be applied and deleted.

**No amount of new code closes these.** They are records of real runs by real
users. This is the single most important thing to understand about "releasing
v5": the gate is asking for evidence, not features.

Consequence for sponsors: shipping `5.0.0` requires a live-run and beta phase.
What can be shipped immediately and honestly is a **release candidate** with a
large, verifiable feature set. That is the recommended framing.

## 3. Scope decision to be recorded

`docs/V5_RELEASE_SCOPE.md` currently lists under *Deferred beyond 5.0*:

- Pack or Skill marketplace;
- arbitrary website automation.

If the Marketplace ships, that document must be edited in the same change that
implements it, so the contract never lies. Website automation stays deferred:
the Marketplace only *distributes* Skills, and computer-use stays bounded
(see 5.2). We do not silently widen the automation promise.

## 4. CLI surface to add

New command group `karox market`, mirroring the existing `pack`/`skill` style
(every subcommand takes `--json`, like all current commands):

| Command | Purpose |
| --- | --- |
| `karox market sources list` | show configured catalog sources |
| `karox market sources add <url>` | register a catalog (Git repo raw URL or file) |
| `karox market sources remove <name>` | unregister a catalog |
| `karox market refresh` | fetch catalogs, verify, cache locally |
| `karox market search <query>` | search cached cards by name/tag/description |
| `karox market show <id>` | full card: source repo, licence, permissions, hash |
| `karox market install <id>` | download, verify, install into global Skills dir |
| `karox market remove <id>` | uninstall a marketplace-installed Skill |
| `karox market list` | list installed marketplace Skills and versions |
| `karox market doctor` | re-verify installed content against recorded hashes |
| `karox market publish <dir>` | produce a card entry for a Skill you wrote |

Design constraints, each deliberate:

- **A card is a pointer, not a payload.** As requested: the catalog stores a
  repository URL, subdirectory, ref/commit, and metadata. Content is fetched on
  install.
- **Pin by commit, verify by hash.** A card records an expected content hash. A
  moving branch that changes content fails verification instead of installing
  silently. This is the difference between a marketplace and a supply-chain
  hole.
- **Install is never implicit.** `market install` requires explicit approval of
  any declared permission, reusing the Pack approval model.
- **Skills install as data, not code.** A Skill is instructions plus references.
  It does not become an executable extension. This keeps
  "automatic execution of arbitrary extension code" genuinely deferred.
- **Offline honesty.** `market refresh` needs network; `search`/`show` work from
  cache and say when the cache is stale.

## 5. Skills to ship

### 5.1 `frontend-design`
Pure-instruction Skill: layout, spacing, type scale, colour/contrast,
accessibility, component states, responsive behaviour, design review checklist.
No new capability, no permission. Zero risk, immediate visible value.

### 5.2 `computer-use`
Bounded, and the boundary is the feature. It uses the existing
`browser.read`/`browser.input` capabilities and the existing Playwright path,
and keeps the localhost-only restriction for verifying the user's own app:
navigate, inspect, click, fill, assert text, screenshot. It documents plainly
that it drives a local development app, not arbitrary websites. `desktop.input`
stays unimplemented — claiming OS-level control would need a sandbox story the
product explicitly disclaims.

### 5.3 Additional Skills (my judgement, all instruction-only)
`code-review`, `debugging`, `testing-strategy`, `api-design`,
`database-schema`, `performance`, `security-review`, `refactoring`,
`git-workflow`, `documentation`.

Ten instruction Skills plus two above gives a genuinely full catalogue with no
new attack surface — the honest way to make the list long.

## 6. MCP extensions

- `karox mcp tools list` — every tool across selected servers in one view.
- `karox mcp export` — emit client config for ChatGPT/Claude/other MCP clients.
- `karox mcp doctor <server>` — connectivity, auth, tool-count diagnosis.
- Widen `agent_mcp.py` so an external MCP client can drive Skills and the
  Marketplace, subject to the same policy.

## 7. Per-connector Skill support

Requested: every connector and API should be able to use Skills. Concretely,
Skill selection must be persisted per session and injected into the system
prompt for **all** paths — native providers, ChatGPT Web, Claude Web, MCP —
not only the native agent. Verified by a test per path.

## 8. TUI changes

The TUI is how a sponsor will actually see this.

1. **Marketplace screen** — browse/search cards, permissions shown before
   install, install/remove, source repository visible.
2. **Skills screen** — installed Skills, toggle per session, see what is active.
3. **Status line** — show active Skills count next to model/bridge state.
4. **Connect flow** — after connecting ChatGPT Web, offer recommended Skills.

All new screens follow the existing `ModalScreen` pattern and bilingual
`_label(ru, en)` copy, so `check_user_facing_copy.py` keeps passing.

## 9. Gates that will react

Every one of these must stay green, and each new command/doc claim must satisfy:

- `check_test_count.py` — published counts must be updated with new tests.
- `check_documented_commands.py` — docs may not name a command that does not
  exist in argparse.
- `check_dependencies.py` — any new import must be declared.
- `check_user_facing_copy.py` — bilingual copy rules.
- `check_access_profiles.py` — new capability wiring.
- `check_v5_release.py` — scope document consistency.

## 10. Order of work

1. Scope + docs edit (Marketplace out of deferred) — makes everything else legal.
2. `market` core module: catalog model, cache, hash verification, install/remove.
3. `market` CLI wiring + tests.
4. `frontend-design` + 10 instruction Skills.
5. `computer-use` Skill on the existing bounded browser path.
6. Skill injection for every connector + per-path tests.
7. MCP additions.
8. TUI Marketplace/Skills screens.
9. Docs, counts, all gates, full suite.
10. Tag a release candidate; open the live-evidence and beta phase.

Steps 1-3 are the risky part and get the most test attention. Steps 4-5 are
where the visible product value appears.

## 11. What this plan does not claim

- It does not claim `5.0.0` final can be tagged without live runs and beta.
- It does not add arbitrary website automation.
- It does not execute third-party code from the Marketplace.
- It does not claim OS sandboxing.
