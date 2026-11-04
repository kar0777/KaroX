#!/bin/sh
# KaroX 6 installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/kar0777/KaroX/v6/plan-impl-20261104/scripts/install.sh | sh
#
# What it does
#   * finds Python >= 3.10 and pipx (installs pipx for the user if it is missing);
#   * KaroX 5 already installed (pipx venv "karox-runtime" with a 5.x version):
#     installs KaroX 6 next to it as "karox6" (pipx --suffix 6) with its own
#     data channel (KAROX_CHANNEL=dogfood), so v5, its bridge and its data stay
#     untouched;
#   * KaroX 6 already installed: leaves it alone unless KAROX_REINSTALL=1;
#   * otherwise installs KaroX 6 as "karox".
#
# Settings (environment variables)
#   KAROX_SPEC          what to install (pip requirement or URL); default: the
#                       source archive of KAROX_REF, until 6.0 is on PyPI
#   KAROX_REF           git branch for the default KAROX_SPEC
#   KAROX_SIDE_BY_SIDE  auto (default) | 1 (always karox6) | 0 (never)
#   KAROX_REINSTALL     1 to replace an existing KaroX 6
#   KAROX_PIPX          pipx command to use instead of discovering one
#   KAROX_DRY_RUN       1 to print the commands instead of running them
set -eu

REF="${KAROX_REF:-v6/plan-impl-20261104}"
SPEC="${KAROX_SPEC:-https://github.com/kar0777/KaroX/archive/refs/heads/${REF}.zip}"
SIDE_BY_SIDE="${KAROX_SIDE_BY_SIDE:-auto}"
REINSTALL="${KAROX_REINSTALL:-0}"
DRY_RUN="${KAROX_DRY_RUN:-0}"
CHANNEL_PTH="karox6_channel.pth"

say() { printf '%s\n' "karox-install: $*"; }
fail() { printf '%s\n' "karox-install: error: $*" >&2; exit 1; }
run() {
    if [ "$DRY_RUN" = "1" ]; then
        say "would run: $*"
        return 0
    fi
    "$@"
}

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done
[ -n "$PYTHON" ] || fail "Python 3.10 or newer was not found; install it first (https://www.python.org/downloads/)"

if [ -n "${KAROX_PIPX:-}" ]; then
    set -- "$KAROX_PIPX"
elif command -v pipx >/dev/null 2>&1; then
    set -- pipx
elif "$PYTHON" -m pipx --version >/dev/null 2>&1; then
    set -- "$PYTHON" -m pipx
else
    say "pipx not found; installing it for this user"
    if ! run "$PYTHON" -m pip install --user --quiet pipx; then
        fail "could not install pipx with pip (an externally managed Python?). Install it with your package manager (brew install pipx / apt install pipx) and run this again"
    fi
    set -- "$PYTHON" -m pipx
fi
PIPX_CMD="$*"

# Versions of the two pipx venvs KaroX can occupy, "" when absent.
LISTING=$("$@" list --json 2>/dev/null || printf '{}')
versions=$(printf '%s' "$LISTING" | "$PYTHON" -c '
import json, sys
try:
    venvs = json.load(sys.stdin).get("venvs", {})
except ValueError:
    venvs = {}
def version(name):
    meta = venvs.get(name, {}).get("metadata", {}).get("main_package", {})
    return str(meta.get("package_version") or "")
print(version("karox-runtime") + "|" + version("karox-runtime6"))
')
MAIN_VERSION=${versions%%|*}
SIDE_VERSION=${versions#*|}

case "$MAIN_VERSION" in
    5.*|4.*) HAS_V5=1 ;;
    *) HAS_V5=0 ;;
esac
if [ "$HAS_V5" = "0" ] && [ -z "$MAIN_VERSION" ] && command -v karox >/dev/null 2>&1; then
    # KaroX 5 installed some other way (pip --user, an editable checkout).
    case "$(karox --version 2>/dev/null || true)" in
        *" 5."*|5.*) HAS_V5=1 ;;
    esac
fi

case "$SIDE_BY_SIDE" in
    1|yes|true) SUFFIXED=1 ;;
    0|no|false)
        [ "$HAS_V5" = "1" ] && fail "KaroX 5 is installed; installing KaroX 6 without a suffix would replace it. Use KAROX_SIDE_BY_SIDE=1 or uninstall KaroX 5 first"
        SUFFIXED=0 ;;
    *) SUFFIXED=$HAS_V5 ;;
esac

if [ "$SUFFIXED" = "1" ]; then
    VENV="karox-runtime6"; COMMAND="karox6"; EXISTING="$SIDE_VERSION"
else
    VENV="karox-runtime"; COMMAND="karox"; EXISTING="$MAIN_VERSION"
fi

if [ -n "$EXISTING" ] && [ "$REINSTALL" != "1" ]; then
    say "KaroX $EXISTING is already installed as '$COMMAND' (pipx venv $VENV); nothing changed."
    say "Set KAROX_REINSTALL=1 to replace it with $SPEC"
    exit 0
fi

FORCE=""
[ -n "$EXISTING" ] && FORCE="--force"
if [ "$SUFFIXED" = "1" ]; then
    say "KaroX 5 stays as 'karox'; installing KaroX 6 next to it as 'karox6'"
    # shellcheck disable=SC2086
    run "$@" install $FORCE --suffix 6 "$SPEC" || fail "pipx could not install $SPEC"
    VENVS=$("$@" environment --value PIPX_LOCAL_VENVS 2>/dev/null || true)
    VENV_PYTHON="$VENVS/$VENV/bin/python"
    if [ "$DRY_RUN" = "1" ]; then
        say "would write $CHANNEL_PTH (KAROX_CHANNEL=dogfood) into $VENV"
    elif [ -n "$VENVS" ] && [ -x "$VENV_PYTHON" ]; then
        SITE=$("$VENV_PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
        printf '%s\n' 'import os; os.environ.setdefault("KAROX_CHANNEL", "dogfood")' > "$SITE/$CHANNEL_PTH"
        say "karox6 keeps its data in the separate 'dogfood' channel"
    else
        fail "installed, but the karox6 venv was not found to set its data channel; set KAROX_CHANNEL=dogfood before running karox6"
    fi
else
    # shellcheck disable=SC2086
    run "$@" install $FORCE "$SPEC" || fail "pipx could not install $SPEC"
fi

say "done. Run: $COMMAND --version   (if the command is not found: $PIPX_CMD ensurepath, then open a new terminal)"
