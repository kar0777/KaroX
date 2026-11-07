# Installing KaroX 5.0

KaroX 5.0 is a **local release candidate**. It is not on PyPI yet and must not be
published or installed over a working `karox` until the owner approves the
artifact.

Python requirement: **3.10 or newer**.

## Local wheel (recommended for review)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .\dist\karox_runtime-5.0.0-py3-none-any.whl
karox --version
karox paths --json
karox agent run --help
```

Use an isolated `KAROX_CONFIG_DIR` / `KAROX_RUNTIME_DIR` for the first smoke so a
real user profile is not touched:

```powershell
$env:KAROX_CONFIG_DIR = "$env:TEMP\karox5-review-config"
$env:KAROX_RUNTIME_DIR = "$env:TEMP\karox5-review-runtime"
```

## Backup and upgrade

The stable line reads existing v5 configuration, keyring references, saved
bridge profiles, MCP selections and schema-1 sessions. It writes additional
session fields that older rc4 readers do not understand. Therefore:

1. Stop the TUI, agent runs and local bridge children. Do not stop a bridge you
   do not own and do not reset a Tailscale route.
2. Back up the whole data set:
   - `%APPDATA%\KaroX` (Windows) or `~/.config/KaroX`;
   - the runtime directory shown by `karox paths --json`;
   - saved bridge profiles, tailscale-bin and any per-profile OAuth state.
3. Validate the new wheel in a fresh venv with a **copy** of the data.
4. Run `karox doctor`, `karox session list`, `karox bridge saved list`,
   `karox provider list`, and one small agent task.
5. Replace the installed command only with explicit owner approval.
6. Roll back by restoring the backup. Downgrading to rc4 without restoring the
   pre-upgrade sessions is unsupported because rc4 rejects the newer fields.

Do not delete `%APPDATA%\KaroX`, `%LOCALAPPDATA%\KaroX\vnext`, keyring entries,
saved profiles or user sessions to make an installation succeed.

## Published installers

The historical one-line installers in this tree were written for the 6.x
dogfood line and still default to `v6/plan-impl-20261104`. They are **not** the
5.0.0 release installer and are not used by this candidate. A release installer
must be re-tagged and re-tested before publication; until then install the wheel
locally as above.

When it is ready, the intended published form is a pinned, immutable 5.0.0
artifact (PyPI wheel or a signed/tagged archive), not a mutable branch:

```text
pipx install karox-runtime==5.0.0
uv tool install karox-runtime==5.0.0
```

## Compatible aliases

- `karox` — primary command.
- `karox-vnext` — historical alias, same entrypoint and same policy/session
  surface.
- `karox-agent-mcp` is **not** provided by the stable line; the v5 Ellipsis
  remote-control experiment is out of scope. Do not install a stub that claims
  otherwise.

## Platform status

- Windows: primary tested host.
- Linux: package/tests are platform-neutral, but a real Linux install and
  installer run are **NOT TESTED** in this pass.
- macOS: **NOT TESTED**.

## Settings

For local review the standard KaroX settings apply:

| Variable | Meaning |
| --- | --- |
| `KAROX_CONFIG_DIR` | Isolated configuration directory. |
| `KAROX_RUNTIME_DIR` | Isolated durable runtime/session directory. |
| `KAROX_CHANNEL` | Stable/dogfood data channel; keep `stable` for this release. |
| `KAROX_DISABLE_UPDATE` | `1` disables update checks during review. |
| `KAROX_SPEC` | Only for a purpose-built release installer; the local wheel path is authoritative here. |
