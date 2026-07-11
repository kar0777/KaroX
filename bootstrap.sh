#!/usr/bin/env bash
# Star For KaroX — stable one-command installer for macOS / Linux.
# Downloads the selected GitHub release/ref and runs install.sh.
#
# Latest stable:
#   curl -fsSL https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.sh | bash
# Development override:
#   KAROX_BOOTSTRAP_REF=main bash bootstrap.sh
#
# Compatible with bash 3.2.

set -u

REPO_OWNER="kar0777"
REPO_NAME="KaroX"
REF="${KAROX_BOOTSTRAP_REF:-${REPOPILOT_BOOTSTRAP_BRANCH:-v3.12.3}}"
INSTALL_ROOT="${REPOPILOT_INSTALL_ROOT:-$HOME/.local/share/RepoPilotBridge}"
SOURCE_DIR="$INSTALL_ROOT/source"

case "$REF" in
    v[0-9]*.[0-9]*.[0-9]*) REF_KIND="tags" ;;
    *) REF_KIND="heads" ;;
esac

C_GREEN=$'\033[32m'; C_CYAN=$'\033[36m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_RESET=$'\033[0m'
log_info()    { printf '%s%s%s\n' "$C_CYAN" "$1" "$C_RESET"; }
log_success() { printf '%s%s%s\n' "$C_GREEN" "$1" "$C_RESET"; }
log_warn()    { printf '%s%s%s\n' "$C_YELLOW" "$1" "$C_RESET"; }
log_error()   { printf '%s%s%s\n' "$C_RED" "$1" "$C_RESET" >&2; }

has_command() { command -v "$1" >/dev/null 2>&1; }

echo ""
log_info "Star For KaroX — stable bootstrap"
printf '%s\n' '----------------------------------------'
printf 'Repository : https://github.com/%s/%s\n' "$REPO_OWNER" "$REPO_NAME"
printf 'Release/ref: %s\n\n' "$REF"

if ! has_command curl; then
    log_error "curl was not found. Install curl and retry."
    exit 1
fi
if ! has_command tar; then
    log_error "tar was not found. Install tar and retry."
    exit 1
fi

mkdir -p "$INSTALL_ROOT"

# When bootstrap.sh is already inside a complete source checkout, use it directly.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
if [ -f "$SCRIPT_DIR/install.sh" ] && [ -f "$SCRIPT_DIR/server/repo_tools.py" ]; then
    log_info "Using local source: $SCRIPT_DIR"
    SOURCE_DIR="$SCRIPT_DIR"
else
    ARCHIVE_URL="https://github.com/$REPO_OWNER/$REPO_NAME/archive/refs/$REF_KIND/$REF.tar.gz"
    log_info "Downloading verified archive..."
    log_info "$ARCHIVE_URL"
    rm -rf "$SOURCE_DIR" 2>/dev/null || true
    mkdir -p "$SOURCE_DIR"
    archive="$(mktemp -t karox-bootstrap-XXXXXX.tar.gz 2>/dev/null || mktemp).tar.gz"
    if ! curl -fsSL "$ARCHIVE_URL" -o "$archive"; then
        log_error "Could not download KaroX. Check the release/ref and your internet connection."
        log_error "URL: $ARCHIVE_URL"
        rm -f "$archive" 2>/dev/null || true
        exit 1
    fi
    log_info "Extracting into $SOURCE_DIR ..."
    if ! tar -xzf "$archive" -C "$SOURCE_DIR" --strip-components=1 2>/dev/null; then
        if ! tar -xzf "$archive" -C "$SOURCE_DIR" 2>/dev/null; then
            log_error "Could not extract the KaroX archive."
            rm -f "$archive" 2>/dev/null || true
            exit 1
        fi
        sub="$(find "$SOURCE_DIR" -maxdepth 1 -type d -name "$REPO_NAME-*" | head -n1 || true)"
        if [ -n "$sub" ]; then
            (cd "$sub" && tar cf - .) | (cd "$SOURCE_DIR" && tar xf -) 2>/dev/null || true
            rm -rf "$sub" 2>/dev/null || true
        fi
    fi
    rm -f "$archive" 2>/dev/null || true
fi

if [ ! -f "$SOURCE_DIR/install.sh" ]; then
    log_error "install.sh was not found after extraction: $SOURCE_DIR/install.sh"
    exit 1
fi

log_success "KaroX files are ready: $SOURCE_DIR"
echo ""
if [ "${KAROX_NO_START:-0}" = "1" ]; then
    log_info "Updating KaroX without opening Flight Deck..."
    bash "$SOURCE_DIR/install.sh"
else
    log_info "Starting install.sh --start ..."
    bash "$SOURCE_DIR/install.sh" --start
fi
exit $?
