#!/bin/sh
# KaroX 5.0.0 installer for macOS and Linux.
#
#   KAROX_SPEC=/path/to/karox_runtime-5.0.0-py3-none-any.whl \
#     sh scripts/install.sh
#
# What it does
#   * finds pipx (or uses KAROX_PIPX);
#   * fresh machine: installs KaroX 5.0.0 as "karox";
#   * existing KaroX 5.x/6.x: refuses to overwrite unless KAROX_UPGRADE=1; then
#     copies the KaroX data directories to timestamped backups before
#     `pipx install --force`;
#   * leaves an old side-by-side karox-runtime6 venv untouched.
#
# Settings:
#   KAROX_SPEC      required: pinned 5.0.0 requirement, URL or local wheel
#   KAROX_UPGRADE   1 to replace an existing main install after backup
#   KAROX_PIPX      pipx executable path
#   KAROX_DRY_RUN   1 to print commands/backups instead of running them
set -eu

SPEC="${KAROX_SPEC:-}"
UPGRADE="${KAROX_UPGRADE:-0}"
DRY_RUN="${KAROX_DRY_RUN:-0}"

say() { printf '%s\n' "karox-install: $*"; }
fail() { printf '%s\n' "karox-install: error: $*" >&2; exit 1; }
run() {
    if [ "$DRY_RUN" = "1" ]; then
        say "would run: $*"
        return 0
    fi
    "$@"
}

[ -n "$SPEC" ] || fail "KAROX_SPEC is required; point it at karox_runtime-5.0.0-py3-none-any.whl (or a pinned 5.0.0 requirement)"

if [ -n "${KAROX_PIPX:-}" ]; then
    PIPX="$KAROX_PIPX"
elif command -v pipx >/dev/null 2>&1; then
    PIPX="$(command -v pipx)"
elif command -v python3 >/dev/null 2>&1; then
    PIPX="python3 -m pipx"
elif command -v python >/dev/null 2>&1; then
    PIPX="python -m pipx"
else
    fail "Python 3.10+ with pipx was not found; install pipx first"
fi

# A command with spaces (python -m pipx) is used only for the main install;
# tests and the documented path pass an executable path through KAROX_PIPX.
pipx_cmd() {
    if [ "$PIPX" = "python3 -m pipx" ]; then
        python3 -m pipx "$@"
    elif [ "$PIPX" = "python -m pipx" ]; then
        python -m pipx "$@"
    else
        "$PIPX" "$@"
    fi
}

LISTING="$(pipx_cmd list --json 2>/dev/null || true)"
MAIN_VERSION=""
SIDE_VERSION=""
if [ -n "$LISTING" ]; then
    VERSIONS="$(printf '%s' "$LISTING" | python3 -c '
import json, sys
try:
    venvs = json.load(sys.stdin).get("venvs", {})
except ValueError:
    venvs = {}
def version(name):
    meta = venvs.get(name, {}).get("metadata", {}).get("main_package", {})
    return str(meta.get("package_version") or "")
print(version("karox-runtime") + "|" + version("karox-runtime6"))
' 2>/dev/null || printf '|')"
    MAIN_VERSION="${VERSIONS%%|*}"
    SIDE_VERSION="${VERSIONS#*|}"
fi

EXTERNAL=0
if [ -z "$MAIN_VERSION" ] && command -v karox >/dev/null 2>&1; then
    case "$(karox --version 2>/dev/null || true)" in
        *" 4."*|*" 5."*|*" 6."*|4.*|5.*|6.*) EXTERNAL=1 ;;
    esac
fi
if [ -n "$SIDE_VERSION" ]; then
    say "an old side-by-side karox6 install ($SIDE_VERSION) is present; leaving it untouched"
fi

if [ -n "$MAIN_VERSION" ] || [ "$EXTERNAL" = "1" ]; then
    EXISTING="${MAIN_VERSION:-outside pipx}"
    if [ "$UPGRADE" != "1" ]; then
        say "KaroX $EXISTING is already installed; nothing changed."
        say "Back up the data and set KAROX_UPGRADE=1 to install $SPEC over it."
        exit 0
    fi
    say "KAROX_UPGRADE=1: backing up KaroX data before replacing $EXISTING"
    STAMP="$(date +%Y%m%d-%H%M%S 2>/dev/null || printf manual)"
    for source in \
        "${KAROX_CONFIG_DIR:-}" \
        "${XDG_CONFIG_HOME:-$HOME/.config}/KaroX" \
        "${KAROX_RUNTIME_DIR:-}" \
        "${XDG_DATA_HOME:-$HOME/.local/share}/KaroX"
    do
        [ -n "$source" ] || continue
        [ -e "$source" ] || continue
        destination="$source.backup-$STAMP"
        if [ "$DRY_RUN" = "1" ]; then
            say "would copy $source -> $destination"
        else
            cp -a "$source" "$destination"
            say "backed up $source -> $destination"
        fi
    done
fi

if [ -n "$MAIN_VERSION" ] || [ "$EXTERNAL" = "1" ]; then
    run pipx_cmd install --force "$SPEC" || fail "pipx could not install $SPEC"
else
    run pipx_cmd install "$SPEC" || fail "pipx could not install $SPEC"
fi

say "done. Run: karox --version"
if [ -n "${STAMP:-}" ]; then
    say "rollback: restore the .backup-$STAMP directories before reinstalling the previous wheel"
fi
