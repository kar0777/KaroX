# KaroX v3.14.1 — Doctor rebrand fix

- Fixes a false failure in `karox doctor` after upgrading from RepoPilotBridge to KaroX.
- Preserves the legacy-path detector while installed runtime files are rewritten to the KaroX brand.
- Keeps real stale-path detection: launchers that still point to the old runtime directory continue to fail the doctor check.
- Adds regression coverage for Windows, macOS, Linux, Python 3.10/3.12, and runtime rebranding.
- Does not reset settings, sessions, repository history, or the persistent Notion credential.
