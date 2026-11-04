# Installing KaroX 6

KaroX 6 is not on PyPI yet (the `karox-runtime` name there still carries the
5.0 release candidates), so the installers install from the source archive of
a branch on GitHub. Both need Python 3.10 or newer; pipx is installed for the
user if it is missing.

## One command

Windows (PowerShell 5.1 or 7):

```powershell
irm https://raw.githubusercontent.com/kar0777/KaroX/v6/plan-impl-20261104/scripts/install.ps1 | iex
```

macOS and Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/kar0777/KaroX/v6/plan-impl-20261104/scripts/install.sh | sh
```

## Next to KaroX 5

The installers look for KaroX 5 first (the pipx venv `karox-runtime` with a
5.x version, or a `karox` command that reports 5.x). When it is there, KaroX 6
is installed **next to it**:

| | KaroX 5 | KaroX 6 |
| --- | --- | --- |
| command | `karox` | `karox6` |
| pipx venv | `karox-runtime` | `karox-runtime6` (`pipx install --suffix 6`) |
| data | `%APPDATA%\KaroX` / `~/.config/KaroX` | the `dogfood` channel: `KaroX-dogfood` |

The separate channel comes from a one-line `karox6_channel.pth` in the
`karox-runtime6` venv (`KAROX_CHANNEL=dogfood` unless already set), so v5, its
bridge, its providers and sessions are not touched. Without KaroX 5 the
command is plain `karox` in the stable channel.

An existing KaroX 6 is never replaced silently: the installer reports the
version and stops. `KAROX_REINSTALL=1` replaces it (`pipx install --force`).
A KaroX 6 that is a development install (an editable checkout) should be
updated from its checkout instead.

## Settings

| Variable | Meaning |
| --- | --- |
| `KAROX_SPEC` | What to install: a pip requirement or URL (for a release: `karox-runtime==6.0.0`). |
| `KAROX_REF` | Branch for the default source archive. |
| `KAROX_SIDE_BY_SIDE` | `auto` (default), `1` always `karox6`, `0` never — refuses if that would replace KaroX 5. |
| `KAROX_REINSTALL` | `1` to replace an existing KaroX 6. |
| `KAROX_DRY_RUN` | `1` prints the pipx commands instead of running them. |
| `KAROX_PIPX` | A pipx executable to use instead of the one on `PATH`. |

With PowerShell, set them first: `$env:KAROX_DRY_RUN = "1"; irm … | iex`.

## By hand

```text
pipx install --suffix 6 https://github.com/kar0777/KaroX/archive/refs/heads/<branch>.zip
uv tool install https://github.com/kar0777/KaroX/archive/refs/heads/<branch>.zip   # no v5 on the machine
```

`uv tool` cannot keep two installs of the same package, so with KaroX 5
present use pipx (or the installer).
