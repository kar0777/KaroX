# KaroX v3.15.1 — Windows updater directory-lock fix

## Fixed

- `karox update` no longer leaves the launcher working directory inside `%LOCALAPPDATA%\KaroX\app`.
- The Windows installer moves out of the installed app directory before replacing application files.
- The generated `karox.ps1` launcher preserves the user's current directory.
- Updating from any ordinary PowerShell directory no longer fails with `Cannot remove ... KaroX\app because it is in use`.
- The localized Notion setup wizard from v3.15.0 remains included.

## Upgrade note for affected v3.14.x/v3.15.0 installations

Because the old launcher itself holds the app directory open, install this patch once through the public bootstrap command rather than `karox update`. Future updates can use `karox update` normally.

Existing settings, sessions, repositories, and the persistent Notion token are preserved.
