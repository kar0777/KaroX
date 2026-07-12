# KaroX v3.14.2 — Windows PATH migration fix

- Replaces the legacy RepoPilotBridge PATH entry with the new KaroX bin directory.
- Prepends `%LOCALAPPDATA%\KaroX\bin` to the persistent user PATH.
- Installs a temporary compatibility launcher in the old bin directory so the current PowerShell window immediately forwards `karox` to the new installation.
- Keeps only the compatibility shim while old processes are still active.
- Removes the remaining legacy runtime directory automatically after KaroX is launched from a fresh shell that no longer contains the old PATH entry.
- Preserves settings, sessions, repository history, and the persistent Notion credential.
- Adds regression coverage for the runtime rebrand and Windows command migration.
