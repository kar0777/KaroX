#!/usr/bin/env bash
set -u

case "$(uname -s)" in
  Darwin)
    CONFIG_DIR="$HOME/Library/Application Support/KaroX"
    LEGACY_CONFIG_DIR="$HOME/Library/Application Support/RepoPilotBridge"
    RUNTIME_DIR="$HOME/.local/share/KaroX"
    LEGACY_RUNTIME_DIR="$HOME/.local/share/RepoPilotBridge"
    DESKTOP_SHORTCUT="$HOME/Desktop/KaroX.command" ;;
  Linux)
    CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/KaroX"
    LEGACY_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/RepoPilotBridge"
    RUNTIME_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/KaroX"
    LEGACY_RUNTIME_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/RepoPilotBridge"
    DESKTOP_SHORTCUT="$HOME/Desktop/KaroX.desktop" ;;
  *)
    CONFIG_DIR="$HOME/.config/KaroX"
    LEGACY_CONFIG_DIR="$HOME/.config/RepoPilotBridge"
    RUNTIME_DIR="$HOME/.local/share/KaroX"
    LEGACY_RUNTIME_DIR="$HOME/.local/share/RepoPilotBridge"
    DESKTOP_SHORTCUT="$HOME/Desktop/KaroX.sh" ;;
esac

printf '\nUninstall KaroX\n'
printf 'Remove local settings, sessions and runtime files? [y/N] '
read -r answer
case "$answer" in [YyДд]*) ;; *) exit 0 ;; esac

if command -v pgrep >/dev/null 2>&1; then
  for pattern in 'uvicorn.*repo_tools:app' 'uvicorn.*notion_entry:app' 'cloudflared.*tunnel' 'tailscale.*funnel'; do
    pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
    [ -z "$pids" ] || kill -9 $pids 2>/dev/null || true
  done
fi
rm -rf "$CONFIG_DIR" "$RUNTIME_DIR" "$LEGACY_CONFIG_DIR" "$LEGACY_RUNTIME_DIR" 2>/dev/null || true
rm -f "$HOME/.local/bin/karox" "$HOME/.local/bin/repopilot" "$DESKTOP_SHORTCUT" 2>/dev/null || true
for rc in "$HOME/.zshrc" "$HOME/.bash_profile" "$HOME/.bashrc"; do
  [ -f "$rc" ] || continue
  tmp="$(mktemp)"
  grep -v -E 'karox-bin-path|repopilot-bin-path' "$rc" > "$tmp" 2>/dev/null || true
  mv "$tmp" "$rc"
done
printf 'KaroX local files were removed.\n'
