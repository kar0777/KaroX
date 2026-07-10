#!/usr/bin/env bash
# Star For KaroX — деинсталлятор для macOS / Linux (POSIX).
# Порт uninstall.ps1. Удаляет runtime, конфиг, шим, ярлык. Останавливает процессы.
# Совместим с bash 3.2.

set -u

case "$(uname -s)" in
    Darwin)
        CONFIG_DIR="$HOME/Library/Application Support/RepoPilotBridge"
        RUNTIME_DIR="$HOME/.local/share/RepoPilotBridge"
        DESKTOP_SHORTCUT="$HOME/Desktop/KaroX.command"
        ;;
    Linux)
        CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/RepoPilotBridge"
        RUNTIME_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/RepoPilotBridge"
        DESKTOP_SHORTCUT="$HOME/Desktop/KaroX.desktop"
        ;;
    *)
        CONFIG_DIR="$HOME/.repopilot-bridge"
        RUNTIME_DIR="$HOME/.repopilot-bridge-data"
        DESKTOP_SHORTCUT="$HOME/Desktop/KaroX.sh"
        ;;
esac
LOCAL_BIN="$HOME/.local/bin"
KAROX_SHIM="$LOCAL_BIN/karox"
REPOPILOT_SHIM="$LOCAL_BIN/repopilot"

C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_RESET=$'\033[0m'
log_success() { printf '%s%s%s\n' "$C_GREEN" "$1" "$C_RESET"; }
log_warn()    { printf '%s%s%s\n' "$C_YELLOW" "$1" "$C_RESET"; }
log_error()   { printf '%s%s%s\n' "$C_RED" "$1" "$C_RESET" >&2; }

ask_yes() {
    printf '%s [Д/н] ' "$1" >&2
    local answer; read -r answer
    [ -z "$answer" ] && return 0
    case "$answer" in [ДдYy]*) return 0 ;; *) return 1 ;; esac
}

echo ""
log_warn "Удаление Star For KaroX"
echo "Будут удалены:"
echo "  - $RUNTIME_DIR (venv, app, sessions, logs)"
echo "  - $CONFIG_DIR (repos.json, settings.json)"
echo "  - $KAROX_SHIM"
echo "  - $REPOPILOT_SHIM"
echo "  - $DESKTOP_SHORTCUT"
echo "  - строка PATH из ~/.zshrc / ~/.bash_profile"
echo ""
if ! ask_yes "Продолжить?"; then
    echo "Отменено."
    exit 0
fi

# Останавливаем запущенные uvicorn / туннели.
echo ""
echo "Останавливаю процессы KaroX..."
if command -v pgrep >/dev/null 2>&1; then
    pids="$(pgrep -f "uvicorn.*repo_tools:app" 2>/dev/null || true)"
    [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
    pids="$(pgrep -f "cloudflared.*tunnel.*localhost" 2>/dev/null || true)"
    [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
    pids="$(pgrep -f "tailscale.*funnel" 2>/dev/null || true)"
    [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
fi
if command -v taskkill >/dev/null 2>&1; then
    # Windows Git Bash fallback.
    for pat in "uvicorn" "cloudflared" "tailscale"; do
        pids="$(tasklist 2>/dev/null | grep -i "$pat" | awk '{print $2}' || true)"
        for p in $pids; do taskkill //PID "$p" //F >/dev/null 2>&1 || true; done
    done
fi

# Удаляем каталоги.
rm -rf "$RUNTIME_DIR" 2>/dev/null && log_success "Удалён runtime: $RUNTIME_DIR" || log_warn "Runtime не найден: $RUNTIME_DIR"
rm -rf "$CONFIG_DIR" 2>/dev/null && log_success "Удалён конфиг: $CONFIG_DIR" || log_warn "Конфиг не найден: $CONFIG_DIR"

# Шим.
rm -f "$KAROX_SHIM" "$REPOPILOT_SHIM" 2>/dev/null && log_success "Удалены команды karox и repopilot" || true

# Ярлык.
rm -f "$DESKTOP_SHORTCUT" 2>/dev/null && log_success "Удалён ярлык: $DESKTOP_SHORTCUT" || true

# Чистим PATH из rc-файлов (idempotent — по маркеру).
MARKER='repopilot-bin-path'
for rc in "$HOME/.zshrc" "$HOME/.bash_profile" "$HOME/.bashrc"; do
    [ -f "$rc" ] || continue
    if grep -q "$MARKER" "$rc" 2>/dev/null; then
        # Удаляем строки с маркером.
        tmp="$(mktemp -t rpuninst-XXXXXX 2>/dev/null || mktemp)"
        grep -v "$MARKER" "$rc" > "$tmp" 2>/dev/null || true
        mv "$tmp" "$rc" 2>/dev/null || cp "$tmp" "$rc"
        log_success "Очищен PATH-маркер в $rc"
    fi
done

echo ""
log_success "Удаление завершено."
echo "Запустите install.sh снова, чтобы переустановить Star For KaroX."
