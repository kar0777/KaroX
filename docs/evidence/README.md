# Evidence records

Dated measurements of a specific commit, kept so that a later claim about KaroX
can be checked against something rather than believed.

These are **not** conformance records. `docs/conformance/` holds live third-party
product runs and is what `scripts/check_v5_release.py` reads; a record here has no
effect on any release gate. The distinction matters: evidence in this directory is
reproducible locally by anyone with the repository, while a conformance record
depends on an account, a provider and a human, and cannot be regenerated on
demand.

## Rules

A record names the commit, the platform, and the exact command behind every
number. A number without a command beside it does not belong here.

Records are append-only. When something is measured again, add a new dated file
rather than editing an old one — the point of a baseline is that it still says
what was true then, including where it was worse.

A record states what failed as plainly as what passed, and what a figure does not
cover. Coverage that excludes subprocess execution, or a suite measured on one
operating system, is worth recording only if it says so.

## Records

| Date | Commit | What it covers |
|---|---|---|
| [2026-07-31](baseline-2026-07-31.md) | `dee041c` | Starting state for the 5.0 work: suite, 12 static gates, lint, types, coverage, and the live evidence still open. |
| [2026-08-07 local acceptance](local-autonomy-acceptance-2026-08-07.md) | `1b01350` + explicitly dirty working tree | Managed full suite, branch coverage, lifecycle survival, wheel/install autonomy, support-bundle gate, and remaining user/remote gates. |
