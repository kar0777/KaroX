#!/usr/bin/env bash
# Star For KaroX — one-shot установщик для macOS / Linux (POSIX).
# Порт bootstrap.ps1. Скачивает архив с GitHub, распаковывает, запускает install.sh.
# Использование:
#   curl -fsSL https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.sh | bash
#   либо локально: bash bootstrap.sh
#
# Совместим с bash 3.2. Не использует winget/chcp.com.

set -u

REPO_OWNER="kar0777"
REPO_NAME="KaroX"
BRANCH="${REPOPILOT_BOOTSTRAP_BRANCH:-main}"
INSTALL_ROOT="${REPOPILOT_INSTALL_ROOT:-$HOME/.local/share/RepoPilotBridge}"
SOURCE_DIR="$INSTALL_ROOT/source"

C_GREEN=$'\033[32m'; C_CYAN=$'\033[36m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_RESET=$'\033[0m'
log_info()    { printf '%s%s%s\n' "$C_CYAN" "$1" "$C_RESET"; }
log_success()  { printf '%s%s%s\n' "$C_GREEN" "$1" "$C_RESET"; }
log_warn()     { printf '%s%s%s\n' "$C_YELLOW" "$1" "$C_RESET"; }
log_error()    { printf '%s%s%s\n' "$C_RED" "$1" "$C_RESET" >&2; }

has_command() { command -v "$1" >/dev/null 2>&1; }

echo ""
log_info "Star For KaroX — bootstrap"
printf '----------------------------------------\n'
echo ""

# Проверяем curl и tar (обязательные для скачивания/распаковки).
if ! has_command curl; then
    log_error "curl не найден. Установите curl и повторите."
    exit 1
fi
if ! has_command tar; then
    log_error "tar не найден. Установите tar и повторите."
    exit 1
fi

mkdir -p "$INSTALL_ROOT"

# Если bootstrap.sh уже внутри распакованного source — не качаем, запускаем install.sh.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
if [ -f "$SCRIPT_DIR/install.sh" ] && [ -f "$SCRIPT_DIR/server/repo_tools.py" ]; then
    log_info "Запуск из исходника: $SCRIPT_DIR"
    SOURCE_DIR="$SCRIPT_DIR"
else
    # Скачиваем tarball-архив (не zip — проще на POSIX без unzip).
    ARCHIVE_URL="https://github.com/$REPO_OWNER/$REPO_NAME/archive/refs/heads/$BRANCH.tar.gz"
    log_info "Скачиваю $ARCHIVE_URL ..."
    rm -rf "$SOURCE_DIR" 2>/dev/null || true
    mkdir -p "$SOURCE_DIR"
    archive="$(mktemp -t rpbootstrap-XXXXXX.tar.gz 2>/dev/null || mktemp).tar.gz"
    if ! curl -fsSL "$ARCHIVE_URL" -o "$archive"; then
        log_error "Не удалось скачать архив. Проверьте URL и подключение к интернету."
        log_error "URL: $ARCHIVE_URL"
        rm -f "$archive" 2>/dev/null
        exit 1
    fi
    log_info "Распаковываю в $SOURCE_DIR ..."
    if ! tar -xzf "$archive" -C "$SOURCE_DIR" --strip-components=1 2>/dev/null; then
        # GNU tar на некоторых системах не поддерживает --strip-components, пробуем без.
        if ! tar -xzf "$archive" -C "$SOURCE_DIR" 2>/dev/null; then
            log_error "Не удалось распаковать архив."
            rm -f "$archive" 2>/dev/null
            exit 1
        fi
        # Если без strip, файлы будут в подпапке — найдём её.
        sub="$(find "$SOURCE_DIR" -maxdepth 1 -type d -name "$REPO_NAME-*" | head -n1 || true)"
        if [ -n "$sub" ]; then
            # Перемещаем содержимое подпапки наверх.
            (cd "$sub" && tar cf - .) | (cd "$SOURCE_DIR" && tar xf -) 2>/dev/null || true
            rm -rf "$sub" 2>/dev/null || true
        fi
    fi
    rm -f "$archive" 2>/dev/null
fi

if [ ! -f "$SOURCE_DIR/install.sh" ]; then
    log_error "install.sh не найден после распаковки: $SOURCE_DIR/install.sh"
    exit 1
fi

log_success "Файлы готовы: $SOURCE_DIR"
echo ""

# Запускаем install.sh с флагом --start.
log_info "Запускаю install.sh --start ..."
bash "$SOURCE_DIR/install.sh" --start
exit $?
