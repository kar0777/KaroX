#!/usr/bin/env bash
set -euo pipefail

DO_START=0
for arg in "$@"; do case "$arg" in --start|-start) DO_START=1 ;; esac; done
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
APP_DIR="$RUNTIME_DIR/app"
BIN_DIR="$RUNTIME_DIR/bin"
VENV_DIR="$RUNTIME_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
LOCAL_BIN="$HOME/.local/bin"
KAROX_SHIM="$LOCAL_BIN/karox"
MIGRATION="$ROOT/scripts/karox_paths.py"
REBRAND="$ROOT/scripts/rebrand_runtime.py"

find_python() {
  for p in python3.13 python3.12 python3 python; do
    command -v "$p" >/dev/null 2>&1 || continue
    if "$p" -c 'import sys,venv; assert sys.version_info >= (3,10)' >/dev/null 2>&1; then command -v "$p"; return 0; fi
  done
  return 1
}

copy_app() {
  rm -rf "$APP_DIR"
  mkdir -p "$APP_DIR"
  shopt -s dotglob nullglob 2>/dev/null || true
  for item in "$ROOT"/*; do
    base="$(basename "$item")"
    case "$base" in .git|.venv|__pycache__) continue ;; esac
    cp -R "$item" "$APP_DIR/"
  done
}

repair_required() {
  for rel in scripts/tailscale_readiness.py scripts/notion_setup_wizard.py scripts/karox_paths.py scripts/karox_admin_entry.py scripts/support_bundle_entry.py scripts/rebrand_runtime.py; do
    if [ ! -e "$APP_DIR/$rel" ] && [ -e "$ROOT/$rel" ]; then mkdir -p "$(dirname "$APP_DIR/$rel")"; cp -f "$ROOT/$rel" "$APP_DIR/$rel"; fi
  done
}

assert_complete() {
  for rel in \
    start.sh start.core.sh requirements.txt \
    scripts/karox_paths.py scripts/karox_admin_entry.py scripts/support_bundle_entry.py scripts/rebrand_runtime.py \
    scripts/notion_profile.py scripts/tailscale_readiness.py scripts/notion_setup_wizard.py \
    server/repo_tools.py server/notion_gateway.py; do
    [ -e "$APP_DIR/$rel" ] || { echo "Incomplete KaroX installation. Missing: $rel" >&2; exit 1; }
  done
  ! grep -q 'RepoPilotBridge' "$APP_DIR/start.sh" || { echo 'Installed start.sh still contains legacy paths.' >&2; exit 1; }
}

printf '\nKaroX installer\n----------------------------------------\n'
mkdir -p "$CONFIG_DIR" "$RUNTIME_DIR" "$BIN_DIR" "$LOCAL_BIN"
BASE_PYTHON="$(find_python || true)"
[ -n "$BASE_PYTHON" ] || { echo 'Python 3.10+ is required.' >&2; exit 1; }
export KAROX_CONFIG_DIR="$CONFIG_DIR"
export KAROX_RUNTIME_DIR="$RUNTIME_DIR"
[ ! -f "$MIGRATION" ] || "$BASE_PYTHON" "$MIGRATION" migrate --json >/dev/null

if [ ! -x "$VENV_PYTHON" ]; then "$BASE_PYTHON" -m venv "$VENV_DIR"; fi
"$VENV_PYTHON" -m pip install --upgrade pip --quiet
"$VENV_PYTHON" -m pip install -r "$ROOT/requirements.txt" --quiet

copy_app
repair_required
"$VENV_PYTHON" "$APP_DIR/scripts/rebrand_runtime.py" --root "$APP_DIR"
assert_complete

cat > "$KAROX_SHIM" <<'EOF'
#!/usr/bin/env bash
export KAROX_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/KaroX"
export KAROX_RUNTIME_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/KaroX"
case "$(uname -s)" in Darwin) export KAROX_CONFIG_DIR="$HOME/Library/Application Support/KaroX" ;; esac
export PATH="$KAROX_RUNTIME_DIR/bin:$PATH"
cd "$KAROX_RUNTIME_DIR/app" || exit 1
exec bash ./start.sh "$@"
EOF
chmod +x "$KAROX_SHIM"

case ":$PATH:" in *":$LOCAL_BIN:"*) ;; *) export PATH="$LOCAL_BIN:$PATH" ;; esac
for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
  [ -f "$rc" ] || continue
  grep -q 'karox-bin-path' "$rc" 2>/dev/null || printf '\nexport PATH="$HOME/.local/bin:$PATH"  # karox-bin-path\n' >> "$rc"
done

case "$(uname -s)" in
  Darwin)
    printf '#!/usr/bin/env bash\nexec %q\n' "$KAROX_SHIM" > "$DESKTOP_SHORTCUT"
    chmod +x "$DESKTOP_SHORTCUT" ;;
  Linux)
    cat > "$DESKTOP_SHORTCUT" <<EOF
[Desktop Entry]
Type=Application
Name=KaroX
Exec=$KAROX_SHIM
Terminal=true
EOF
    ;;
esac

printf '\nInstallation complete.\nApplication : %s\nRuntime     : %s\nConfig      : %s\nCommand     : karox\n\n' "$APP_DIR" "$RUNTIME_DIR" "$CONFIG_DIR"
rm -rf "$LEGACY_CONFIG_DIR" "$LEGACY_RUNTIME_DIR" 2>/dev/null || true
rm -f "$HOME/.local/bin/repopilot" 2>/dev/null || true

if [ "$DO_START" = 1 ]; then exec bash "$APP_DIR/start.sh"; fi
exec "$VENV_PYTHON" "$APP_DIR/scripts/product_doctor.py" --root "$APP_DIR"
