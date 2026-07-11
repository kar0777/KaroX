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
ADMIN="$SCRIPT_DIR/scripts/karox_admin.py"
SUPPORT="$SCRIPT_DIR/scripts/support_bundle.py"
CORE="$SCRIPT_DIR/start.core.sh"
GENERATED_DIR="$RUNTIME_DIR/generated"
GENERATED="$GENERATED_DIR/start.notion.generated.sh"

find_python() {
  if [ -x "$VENV_PYTHON" ]; then printf '%s' "$VENV_PYTHON"; return 0; fi
  command -v python3 2>/dev/null || command -v python 2>/dev/null || return 1
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
    doctor|status)
      exec "$PYTHON_EXE" "$NOTION_DOCTOR" --root "$SCRIPT_DIR"
      ;;
    docs)
      printf '%s\n' "$SCRIPT_DIR/NOTION.md"
      exit 0
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
