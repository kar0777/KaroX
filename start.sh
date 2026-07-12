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
TAILSCALE_READINESS="$SCRIPT_DIR/scripts/tailscale_readiness.py"
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
print('\nKaroX <-> Notion persistent connection')
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

tailscale_json() {
  [ -f "$TAILSCALE_READINESS" ] || { echo "Tailscale readiness module is missing. Run: karox update" >&2; return 1; }
  "$PYTHON_EXE" "$TAILSCALE_READINESS" "$@"
}

json_ready() {
  NOTION_PAYLOAD="$1" "$PYTHON_EXE" -c "import json,os,sys; x=json.loads(os.environ['NOTION_PAYLOAD']); sys.exit(0 if x.get('ready') and x.get('baseUrl') else 1)"
}

save_tailscale_url() {
  local probe="$1" base_url
  base_url="$(NOTION_PAYLOAD="$probe" "$PYTHON_EXE" -c "import json,os; print(json.loads(os.environ['NOTION_PAYLOAD']).get('baseUrl',''))")"
  [ -n "$base_url" ] || return 1
  notion_json set-url --url "$base_url" --json >/dev/null
}

setup_persistent_notion() {
  notion_json ensure --json >/dev/null
  local ts probe up_exit=0
  ts="$(find_tailscale 2>/dev/null || true)"
  if [ -z "$ts" ]; then
    echo "Tailscale is required for a permanent Notion URL." >&2
    echo "Install Tailscale, sign in, then run: karox notion setup" >&2
    return 1
  fi

  probe="$(tailscale_json --wait 3 --json)"
  if ! json_ready "$probe"; then
    echo "Opening Tailscale login..."
    echo "Finish sign-in in the browser or Tailscale app. KaroX will wait up to 120 seconds."
    "$ts" up || up_exit=$?
    echo "Waiting for Tailscale and the stable ts.net hostname..."
    probe="$(tailscale_json --wait 120 --interval 2 --json)"
  fi

  if ! json_ready "$probe"; then
    NOTION_PAYLOAD="$probe" UP_EXIT="$up_exit" "$PYTHON_EXE" - <<'PY' >&2
import json, os
x=json.loads(os.environ['NOTION_PAYLOAD'])
parts=[]
if x.get('backendState'): parts.append('state='+str(x['backendState']))
if x.get('error'): parts.append(str(x['error']))
if os.environ.get('UP_EXIT') not in ('', '0'): parts.append('tailscale up exit='+os.environ['UP_EXIT'])
if x.get('authUrl'): parts.append('login='+str(x['authUrl']))
print('Tailscale setup did not finish. ' + ' | '.join(parts))
print('Complete Tailscale sign-in, wait until the app says Connected, then run: karox notion setup')
PY
    return 1
  fi

  save_tailscale_url "$probe"
  local state
  state="$(notion_json connection --json --show-token)"
  show_notion_connection "$state" 1
  echo "Add this Custom MCP server to Notion once. Future KaroX sessions reuse the same URL and token."
  echo "After connecting, run: karox notion"
}

ensure_persistent_notion() {
  local probe
  probe="$(tailscale_json --wait 5 --json)"
  if ! json_ready "$probe"; then
    setup_persistent_notion
    probe="$(tailscale_json --wait 10 --json)"
  fi
  json_ready "$probe" || { echo "Tailscale is not ready. Run: karox notion setup" >&2; return 1; }
  save_tailscale_url "$probe"
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
      probe="$(tailscale_json --wait 3 --json)"
      if json_ready "$probe"; then save_tailscale_url "$probe"; fi
      payload="$(notion_json connection --json)"
      show_notion_connection "$payload" 0
      NOTION_PAYLOAD="$probe" "$PYTHON_EXE" -c "import json,os; x=json.loads(os.environ['NOTION_PAYLOAD']); print('Tailscale:', x.get('backendState',''), '|', x.get('dnsName','')); print(x.get('error','') if not x.get('ready') else '')"
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
[ -f "$TAILSCALE_READINESS" ] || { echo "Tailscale readiness module is missing. Reinstall or update KaroX." >&2; exit 1; }

if [ "${KAROX_UPDATE_NOTICE:-1}" != "0" ]; then
  "$PYTHON_EXE" "$ADMIN" notice 2>/dev/null || true
fi

mkdir -p "$GENERATED_DIR"
"$PYTHON_EXE" "$PATCHER" --platform shell --source "$CORE" --output "$GENERATED" --root "$SCRIPT_DIR" >/dev/null
chmod +x "$GENERATED"
export KAROX_SOURCE_ROOT="$SCRIPT_DIR"
if [ "$FORCE_NOTION" = 1 ]; then export KAROX_FORCE_AI_CLIENT=notion; fi
exec bash "$GENERATED"
