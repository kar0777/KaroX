#!/usr/bin/env bash
set -euo pipefail

REPO_OWNER="kar0777"
REPO_NAME="KaroX"
REF="${KAROX_BOOTSTRAP_REF:-v3.15.1}"
INSTALL_ROOT="${KAROX_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/KaroX}"
SOURCE_DIR="$INSTALL_ROOT/source"
case "$REF" in v[0-9]*.[0-9]*.[0-9]*) REF_KIND="tags" ;; *) REF_KIND="heads" ;; esac

printf '\nKaroX stable installer\n----------------------------------------\n'
printf 'Repository : https://github.com/%s/%s\nRelease/ref: %s\n\n' "$REPO_OWNER" "$REPO_NAME" "$REF"
command -v curl >/dev/null 2>&1 || { echo 'curl is required.' >&2; exit 1; }
command -v tar >/dev/null 2>&1 || { echo 'tar is required.' >&2; exit 1; }
mkdir -p "$INSTALL_ROOT"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
if [ -f "$SCRIPT_DIR/install.karox.sh" ] && [ -f "$SCRIPT_DIR/server/repo_tools.py" ]; then
  SOURCE_DIR="$SCRIPT_DIR"
else
  archive="$(mktemp -t karox-bootstrap-XXXXXX).tar.gz"
  url="https://github.com/$REPO_OWNER/$REPO_NAME/archive/refs/$REF_KIND/$REF.tar.gz"
  rm -rf "$SOURCE_DIR"
  mkdir -p "$SOURCE_DIR"
  curl -fsSL "$url" -o "$archive"
  tar -xzf "$archive" -C "$SOURCE_DIR" --strip-components=1
  rm -f "$archive"
fi

[ -f "$SOURCE_DIR/install.karox.sh" ] || { echo 'install.karox.sh was not found.' >&2; exit 1; }
if [ "${KAROX_NO_START:-0}" = 1 ]; then
  exec bash "$SOURCE_DIR/install.karox.sh"
else
  exec bash "$SOURCE_DIR/install.karox.sh" --start
fi
