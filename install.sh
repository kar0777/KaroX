#!/usr/bin/env bash
# Star For KaroX — установщик для macOS / Linux (POSIX).
# Порт install.ps1. Создаёт venv, ставит зависимости, копирует файлы приложения,
# регистрирует команду `repopilot` и ярлык на рабочем столе (macOS: .command).
#
# Совместим с bash 3.2 (macOS по умолчанию). Безопасен для повторного запуска.

set -euo pipefail

# --- Параметры ---------------------------------------------------------------
DO_START=0
for arg in "$@"; do
    case "$arg" in
        --start|-start) DO_START=1 ;;
        --help|-h)
            echo "Использование: install.sh [--start]"
            echo "  --start  запустить start.sh после установки"
            exit 0 ;;
    esac
done

# --- Определение путей -------------------------------------------------------
# SCRIPT_DIR — папка с install.sh (исходник или установленная app/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# macOS: конфиг в ~/Library/Application Support, данные в ~/.local/share.
# Linux: всё в ~/.local/share + ~/.config (XDG). Для единообразия используем
# одну пару каталогов на обеих POSIX-системах.
case "$(uname -s)" in
    Darwin)
        CONFIG_DIR="$HOME/Library/Application Support/RepoPilotBridge"
        RUNTIME_DIR="$HOME/.local/share/RepoPilotBridge"
        DESKTOP_SHORTCUT="$HOME/Desktop/RepoPilot.command"
        ;;
    Linux)
        CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/RepoPilotBridge"
        RUNTIME_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/RepoPilotBridge"
        DESKTOP_SHORTCUT="$HOME/Desktop/RepoPilot.desktop"
        ;;
    *)
        CONFIG_DIR="$HOME/.repopilot-bridge"
        RUNTIME_DIR="$HOME/.repopilot-bridge-data"
        DESKTOP_SHORTCUT="$HOME/Desktop/RepoPilot.sh"
        ;;
esac

APP_DIR="$RUNTIME_DIR/app"
BIN_DIR="$RUNTIME_DIR/bin"
VENV_DIR="$RUNTIME_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
LOCAL_BIN="$HOME/.local/bin"
KAROX_SHIM="$LOCAL_BIN/karox"
REPOPILOT_SHIM="$LOCAL_BIN/repopilot"

# --- Хелперы -----------------------------------------------------------------
C_GREEN=$'\033[32m'; C_CYAN=$'\033[36m'; C_YELLOW=$'\033[33m'
C_RED=$'\033[31m'; C_DARK=$'\033[2m'; C_RESET=$'\033[0m'

log_info()    { printf '%s%s%s\n' "$C_CYAN" "$1" "$C_RESET"; }
log_success() { printf '%s%s%s\n' "$C_GREEN" "$1" "$C_RESET"; }
log_warn()    { printf '%s%s%s\n' "$C_YELLOW" "$1" "$C_RESET"; }
log_error()   { printf '%s%s%s\n' "$C_RED" "$1" "$C_RESET" >&2; }

has_command() { command -v "$1" >/dev/null 2>&1; }

ask_yes() {
    # ask_yes <text> [defaultYes(1/0)]
    local text="$1" default_yes="${2:-1}" suffix
    if [ "$default_yes" = 1 ]; then suffix="Д/н"; else suffix="д/Н"; fi
    printf '%s [%s] ' "$text" "$suffix" >&2
    local answer
    read -r answer
    [ -z "$answer" ] && return "$default_yes"
    case "$answer" in
        [ДдYy]*) return 0 ;;
        *) return 1 ;;
    esac
}

# --- Поиск инструментов ------------------------------------------------------
brew_install() {
    # brew_install <formula> <label> [--cask]
    local formula="$1" label="$2"; shift 2
    if has_command "$formula"; then return 0; fi
    if ! has_command brew; then
        log_error "Homebrew не найден. Установите с https://brew.sh и повторите."
        return 1
    fi
    if ask_yes "Установить $label через Homebrew?"; then
        brew install "$@" "$formula"
    fi
}

# Поиск Python 3.12/3.13. macOS: Homebrew (Apple Silicon /opt/homebrew, Intel /usr/local),
# python.org (/Library/Frameworks/...). Без py-лаунчера.
find_python() {
    local -a candidates=()
    # Явные пути Homebrew и python.org.
    case "$(uname -s)" in
        Darwin)
            candidates+=(
                /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12
                /usr/local/bin/python3.13 /usr/local/bin/python3.12
                /Library/Frameworks/Python.framework/Versions/3.13/bin/python3
                /Library/Frameworks/Python.framework/Versions/3.12/bin/python3
            )
            ;;
    esac
    # Системный/PATH python3.
    candidates+=("$(command -v python3 2>/dev/null || true)")
    candidates+=("$(command -v python 2>/dev/null || true)")

    for p in "${candidates[@]}"; do
        [ -z "$p" ] && continue
        [ -x "$p" ] || continue
        # Должен уметь создавать venv (модуль venv доступен).
        if "$p" -c "import sys, venv; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
            printf '%s' "$p"
            return 0
        fi
    done
    return 1
}

find_cloudflared() {
    local -a paths=(
        /opt/homebrew/bin/cloudflared
        /usr/local/bin/cloudflared
    )
    paths+=("$(command -v cloudflared 2>/dev/null || true)")
    for p in "${paths[@]}"; do
        [ -n "$p" ] && [ -x "$p" ] && { printf '%s' "$p"; return 0; }
    done
    return 1
}

# --- Копирование файлов приложения -------------------------------------------
sync_app_files() {
    mkdir -p "$RUNTIME_DIR" "$BIN_DIR"
    # Если запускаем из уже установленной app/ — ничего копировать не нужно.
    local resolved_root resolved_app
    resolved_root="$(cd "$SCRIPT_DIR" && pwd)"
    resolved_app=""
    [ -d "$APP_DIR" ] && resolved_app="$(cd "$APP_DIR" && pwd)"
    if [ "$resolved_root" = "$resolved_app" ]; then
        printf '%s' "$resolved_root"
        return 0
    fi

    rm -rf "$APP_DIR" 2>/dev/null || true
    mkdir -p "$APP_DIR"
    # Копируем всё, кроме мусора. shopt нужен для скрытых файлов.
    local old_nullglob
    old_nullglob="$(shopt -p nullglob 2>/dev/null || true)"
    shopt -s nullglob 2>/dev/null || true
    for item in "$SCRIPT_DIR"/* "$SCRIPT_DIR"/.[!.]* "$SCRIPT_DIR"/..?*; do
        [ -e "$item" ] || continue
        local base
        base="$(basename "$item")"
        case "$base" in
            .git|.venv|__pycache__|.DS_Store) continue ;;
        esac
        cp -R "$item" "$APP_DIR/"
    done
    eval "$old_nullglob" 2>/dev/null || true
    printf '%s' "$APP_DIR"
}

# --- Регистрация PATH --------------------------------------------------------
ensure_local_bin_in_path() {
    # Дополняем ~/.zshrc и ~/.bash_profile (idempotently).
    local marker='repopilot-bin-path'
    local line="export PATH=\"\$HOME/.local/bin:\$PATH\"  # $marker"
    for rc in "$HOME/.zshrc" "$HOME/.bash_profile" "$HOME/.bashrc"; do
        [ -f "$rc" ] || continue
        if ! grep -q "$marker" "$rc" 2>/dev/null; then
            printf '\n%s\n' "$line" >> "$rc"
        fi
    done
    # В текущей сессии тоже.
    case ":$PATH:" in
        *":$LOCAL_BIN:"*) ;;
        *) export PATH="$LOCAL_BIN:$PATH" ;;
    esac
}

# --- Основной процесс --------------------------------------------------------
echo ""
log_info "Установка Star For KaroX"
printf '%s----------------------------------------%s\n' "$C_DARK" "$C_RESET"
echo ""

mkdir -p "$CONFIG_DIR" "$RUNTIME_DIR" "$BIN_DIR" "$LOCAL_BIN"

# Системные зависимости через brew (по запросу).
if ! has_command git; then brew_install git Git || true; fi
if ! find_cloudflared >/dev/null 2>&1; then brew_install cloudflared cloudflared || true; fi
if ! { has_command node && has_command npm; }; then brew_install node Node.js || true; fi

BASE_PYTHON="$(find_python || true)"
if [ -z "$BASE_PYTHON" ]; then
    if [ "$(uname -s)" = Darwin ]; then
        brew_install python@3.12 "Python 3.12" || true
    fi
    BASE_PYTHON="$(find_python || true)"
fi
if [ -z "$BASE_PYTHON" ]; then
    log_error "Python 3.10+ не найден. Установите Python и повторите установку."
    exit 1
fi
log_success "Python: $BASE_PYTHON"

# venv.
if [ ! -x "$VENV_PYTHON" ]; then
    log_warn "Создаю Python virtualenv: $VENV_DIR"
    rm -rf "$VENV_DIR" 2>/dev/null || true
    "$BASE_PYTHON" -m venv "$VENV_DIR"
    if [ ! -x "$VENV_PYTHON" ]; then
        log_error "Не удалось создать virtualenv: $VENV_PYTHON"
        exit 1
    fi
fi

log_warn "Устанавливаю Python-зависимости..."
"$VENV_PYTHON" -m pip install --upgrade pip --quiet
"$VENV_PYTHON" -m pip install -r "$SCRIPT_DIR/requirements.txt" --quiet

# Копируем файлы приложения.
INSTALLED_APP_DIR="$(sync_app_files)"

# Копируем cloudflared в bin/ (как делает Windows-вариант), если доступен.
CF_EXE="$(find_cloudflared || true)"
if [ -n "$CF_EXE" ]; then
    cp -f "$CF_EXE" "$BIN_DIR/cloudflared" 2>/dev/null || true
    chmod +x "$BIN_DIR/cloudflared" 2>/dev/null || true
fi

# Шим `repopilot` в ~/.local/bin.
cat > "$KAROX_SHIM" <<EOF
#!/usr/bin/env bash
# Star For KaroX launcher (сгенерирован install.sh).
export PATH="\$HOME/.local/share/RepoPilotBridge/bin:\$PATH"
cd "\$HOME/.local/share/RepoPilotBridge/app" || exit 1
exec bash "\$HOME/.local/share/RepoPilotBridge/app/start.sh" "\$@"
EOF
chmod +x "$KAROX_SHIM"
ln -sf "$KAROX_SHIM" "$REPOPILOT_SHIM"
ensure_local_bin_in_path

# Ярлык на рабочем столе.
case "$(uname -s)" in
    Darwin)
        cat > "$DESKTOP_SHORTCUT" <<EOF
#!/usr/bin/env bash
cd "\$HOME/.local/share/RepoPilotBridge/app" || exit 1
exec bash ./start.sh
EOF
        chmod +x "$DESKTOP_SHORTCUT"
        ;;
    Linux)
        cat > "$DESKTOP_SHORTCUT" <<EOF
[Desktop Entry]
Type=Application
Name=Star For KaroX
Exec=bash -c 'cd \$HOME/.local/share/RepoPilotBridge/app && bash ./start.sh'
Terminal=true
EOF
        ;;
esac

echo ""
log_success "Установка завершена."
echo "Приложение       : $INSTALLED_APP_DIR"
echo "Runtime          : $RUNTIME_DIR"
echo "Команда          : karox"
echo "Ярлык            : $DESKTOP_SHORTCUT"
echo ""
log_warn "Если команда karox не находится в текущем терминале — откройте новую вкладку."
echo ""

if [ "$DO_START" = 1 ]; then
    log_success "Запускаю KaroX..."
    bash "$INSTALLED_APP_DIR/start.sh"
    exit $?
fi

if ask_yes "Запустить doctor-проверку сейчас?" 1; then
    bash "$INSTALLED_APP_DIR/doctor.sh" || log_warn "doctor нашёл проблемы (см. вывод выше)."
fi
