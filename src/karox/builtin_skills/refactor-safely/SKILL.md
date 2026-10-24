---
name: refactor-safely
version: 1.0.0
description: Restructure code without changing behavior.
when_to_use: rename, extract, move, split a module, remove duplication.
triggers: [refactor, rename, extract]
---
# Refactor recipe
- Confirm there are tests that cover the code; run them first and keep the result as the baseline.
- Find every usage with repo_search before renaming or moving anything (imports, strings, docs, configs).
- Change in small steps; run the tests after each step. Never mix a refactor with a behavior change.
- Keep public APIs, or leave a thin compatibility alias if callers outside the repo may use them.
- Report: what moved where, the test runs before and after.
