#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "$(uname -s)" in
  Darwin) RUNTIME_DIR="$HOME/.local/share/RepoPilotBridge" ;;
  *) RUNTIME_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/RepoPilotBridge" ;;
esac
VENV_PYTHON="$RUNTIME_DIR/.venv/bin/python"
PATCHER="$SCRIPT_DIR/scripts/patch_notion_provider.py"
NOTION_DOCTOR="$SCRIPT_DIR/scripts/notion_doctor.py"
NOTION_PROFILE="$SCRIPT_DIR/scripts/notion_profile.py"
ADMIN="$SCRIPT_DIR/scripts/karox_cli.py"
SUPPORT="$SCRIPT_DIR/scripts/support_bundle.py"
CORE="$SCRIPT_DIR/start.core.sh"
GENERATED_DIR="$RUNTIME_DIR/generated"
GENERATED="$GENERATED_DIR/start.notion.generated.sh"

find_python() {
  if [ -x "$VENV_PYTHON" ]; then printf '%s' "$VENV_PYTHON"; return 0; fi
  command -v python3 2>/dev/null || command -v python 2>/dev/null || return 1
}

find_tailscale() {
  local candidate=""
  candidate="$(command -v tailscale 2>/dev/null || true)"
  [ -n "$candidate" ] && [ -x "$candidate" ] && { printf '%s' "$candidate"; return 0; }
  for candidate in \
    /Applications/Tailscale.app/Contents/MacOS/Tailscale \
    /opt/homebrew/bin/tailscale \
    /usr/local/bin/tailscale \
    /usr/bin/tailscale; do
    [ -x "$candidate" ] && { printf '%s' "$candidate"; return 0; }
  done
  return 1
}

show_notion_connection() {
  local payload="$1" show_token="${2:-0}"
  NOTION_PAYLOAD="$payload" SHOW_TOKEN="$show_token" "$PYTHON_EXE" - <<'PY'
import json, os
x=json.loads(os.environ['NOTION_PAYLOAD'])
print('\nKaroX ↔ Notion persistent connection')
print('----------------------------------------')
print('MCP URL :', x.get('mcpUrl',''))
print('Auth    : Bearer token')
print('Token   :', x.get('apiKey','') if os.environ.get('SHOW_TOKEN') == '1' else x.get('tokenHint',''))
print()
PY
}

notion_json() {
  [ -f "$NOTION_PROFILE" ] || { echo "Persistent Notion profile module is missing. Run: karox update" >&2; return 1; }
  "$PYTHON_EXE" "$NOTION_PROFILE" "$@"
}

setup_persistent_notion() {
  notion_json ensure --json >/dev/null
  local ts state
  ts="$(find_tailscale 2>/dev/null || true)"
  if [ -z "$ts" ]; then
    echo "Tailscale is required for a permanent Notion URL." >&2
    echo "Install Tailscale, sign in, then run: karox notion setup" >&2
    return 1
  fi
  state="$(notion_json sync-tailscale --json --include-key)"
  if ! NOTION_PAYLOAD="$state" "$PYTHON_EXE" -c "import json,os,sys; sys.exit(0 if json.loads(os.environ['NOTION_PAYLOAD']).get('tailscale',{}).get('ready') else 1)"; then
    echo "Opening Tailscale login..."
    "$ts" up
    state="$(notion_json sync-tailscale --json --include-key)"
  fi
  NOTION_PAYLOAD="$state" "$PYTHON_EXE" -c "import json,os,sys; x=json.loads(os.environ['NOTION_PAYLOAD']); sys.exit(0 if x.get('mcpUrl') and x.get('tailscale',{}).get('ready') else 1)" || {
    echo "Tailscale is not ready or MagicDNS did not provide a stable .ts.net hostname." >&2
    return 1
  }
  show_notion_connection "$state" 1
  echo "Add this Custom MCP server to Notion once. Future KaroX sessions reuse the same URL and token."
}

ensure_persistent_notion() {
  local state
  state="$(notion_json sync-tailscale --json)"
  if ! NOTION_PAYLOAD="$state" "$PYTHON_EXE" -c "import json,os,sys; x=json.loads(os.environ['NOTION_PAYLOAD']); sys.exit(0 if x.get('mcpUrl') and x.get('tailscale',{}).get('ready') else 1)"; then
    setup_persistent_notion
  fi
}

PYTHON_EXE="$(find_python)" || { echo "Python was not found. Run install.sh first." >&2; exit 1; }
FIRST="${1:-}"

case "$FIRST" in
  --version|-v)
    exec "$PYTHON_EXE" "$ADMIN" version
    ;;
  help|--help|-h)
    exec "$PYTHON_EXE" "$ADMIN" --help
    ;;
  support)
    shift
    exec "$PYTHON_EXE" "$SUPPORT" "$@"
    ;;
  version|status|doctor|update|dashboard)
    shift
    exec "$PYTHON_EXE" "$ADMIN" "$FIRST" "$@"
    ;;
esac

FORCE_NOTION=0
if [ "$FIRST" = "notion" ]; then
  FORCE_NOTION=1
  case "${2:-}" in
    install|update)
      "$PYTHON_EXE" -m pip install -r "$SCRIPT_DIR/requirements.txt"
      exec "$PYTHON_EXE" "$NOTION_DOCTOR" --root "$SCRIPT_DIR"
      ;;
    setup)
      setup_persistent_notion
      exit $?
      ;;
    connection)
      payload="$(notion_json connection --json --show-token)"
      show_notion_connection "$payload" 1
      exit 0
      ;;
    rotate-key)
      notion_json rotate --json >/dev/null
      payload="$(notion_json connection --json --show-token)"
      show_notion_connection "$payload" 1
      echo "The key changed. Replace it once in Notion before reconnecting."
      exit 0
      ;;
    reset-connection)
      notion_json reset >/dev/null
      echo "Persistent Notion connection profile removed."
      exit 0
      ;;
    doctor)
      exec "$PYTHON_EXE" "$NOTION_DOCTOR" --root "$SCRIPT_DIR"
      ;;
    status)
      payload="$(notion_json sync-tailscale --json)"
      show_notion_connection "$payload" 0
      exec "$PYTHON_EXE" "$NOTION_DOCTOR" --root "$SCRIPT_DIR"
      ;;
    docs)
      printf '%s\n' "$SCRIPT_DIR/NOTION.md"
      exit 0
      ;;
    "")
      ensure_persistent_notion
      ;;
  esac
fi

[ -f "$CORE" ] || { echo "start.core.sh is missing. Reinstall or update KaroX." >&2; exit 1; }
[ -f "$PATCHER" ] || { echo "Notion provider patcher is missing. Reinstall or update KaroX." >&2; exit 1; }
[ -f "$ADMIN" ] || { echo "KaroX admin CLI is missing. Reinstall or update KaroX." >&2; exit 1; }
[ -f "$SUPPORT" ] || { echo "KaroX support bundle module is missing. Reinstall or update KaroX." >&2; exit 1; }

if [ "${KAROX_UPDATE_NOTICE:-1}" != "0" ]; then
  "$PYTHON_EXE" "$ADMIN" notice 2>/dev/null || true
fi

mkdir -p "$GENERATED_DIR"
"$PYTHON_EXE" "$PATCHER" --platform shell --source "$CORE" --output "$GENERATED" --root "$SCRIPT_DIR" >/dev/null
chmod +x "$GENERATED"
export KAROX_SOURCE_ROOT="$SCRIPT_DIR"
if [ "$FORCE_NOTION" = 1 ]; then export KAROX_FORCE_AI_CLIENT=notion; fi
exec bash "$GENERATED"
