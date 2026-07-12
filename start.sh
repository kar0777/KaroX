#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "$(uname -s)" in
  Darwin) RUNTIME_DIR="${KAROX_RUNTIME_DIR:-$HOME/.local/share/RepoPilotBridge}" ;;
  *) RUNTIME_DIR="${KAROX_RUNTIME_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/RepoPilotBridge}" ;;
esac
VENV_PYTHON="$RUNTIME_DIR/.venv/bin/python"
PATCHER="$SCRIPT_DIR/scripts/patch_notion_provider.py"
NATIVE_PATCHER="$SCRIPT_DIR/scripts/patch_native_notion_provider.py"
NOTION_DOCTOR="$SCRIPT_DIR/scripts/notion_doctor.py"
NOTION_WIZARD="$SCRIPT_DIR/scripts/notion_setup_wizard.py"
ADMIN="$SCRIPT_DIR/scripts/karox_admin_entry.py"
SUPPORT="$SCRIPT_DIR/scripts/support_bundle_entry.py"
CORE="$SCRIPT_DIR/start.core.sh"
GENERATED_DIR="$RUNTIME_DIR/generated"
GENERATED="$GENERATED_DIR/start.notion.generated.sh"

find_python() {
  if [ -x "$VENV_PYTHON" ]; then printf '%s' "$VENV_PYTHON"; return 0; fi
  command -v python3 2>/dev/null || command -v python 2>/dev/null || return 1
}

run_wizard() {
  [ -f "$NOTION_WIZARD" ] || { echo "Localized Notion setup wizard is missing. Run: karox update" >&2; return 1; }
  "$PYTHON_EXE" "$NOTION_WIZARD" "$@"
}

PYTHON_EXE="$(find_python)" || { echo "Python was not found. Run the KaroX installer first." >&2; exit 1; }
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
      run_wizard setup
      exit $?
      ;;
    connection)
      run_wizard connection --show-token
      exit $?
      ;;
    rotate-key)
      run_wizard rotate
      exit $?
      ;;
    reset-connection)
      run_wizard reset
      exit $?
      ;;
    doctor)
      exec "$PYTHON_EXE" "$NOTION_DOCTOR" --root "$SCRIPT_DIR"
      ;;
    status)
      wizard_code=0
      run_wizard status || wizard_code=$?
      "$PYTHON_EXE" "$NOTION_DOCTOR" --root "$SCRIPT_DIR"
      exit "$wizard_code"
      ;;
    docs)
      printf '%s\n' "$SCRIPT_DIR/NOTION.md"
      exit 0
      ;;
    "")
      ;;
    *)
      echo "Unknown notion command: ${2:-}" >&2
      exit 2
      ;;
  esac
fi

for required in "$CORE" "$PATCHER" "$NATIVE_PATCHER" "$ADMIN" "$SUPPORT" "$NOTION_WIZARD"; do
  [ -f "$required" ] || { echo "Required KaroX component is missing: $required. Run: karox update" >&2; exit 1; }
done

if [ "${KAROX_UPDATE_NOTICE:-1}" != "0" ]; then
  "$PYTHON_EXE" "$ADMIN" notice 2>/dev/null || true
fi

mkdir -p "$GENERATED_DIR"
"$PYTHON_EXE" "$PATCHER" --platform shell --source "$CORE" --output "$GENERATED" --root "$SCRIPT_DIR" >/dev/null
"$PYTHON_EXE" "$NATIVE_PATCHER" --platform shell --path "$GENERATED"
chmod +x "$GENERATED"
export KAROX_SOURCE_ROOT="$SCRIPT_DIR"
if [ "$FORCE_NOTION" = 1 ]; then export KAROX_FORCE_AI_CLIENT=notion; fi
exec bash "$GENERATED"
