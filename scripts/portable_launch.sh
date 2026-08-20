#!/usr/bin/env sh
set -eu

# KaroX v5 portable bootstrap. Shipped beside a pinned uv binary and exactly
# one karox_runtime wheel. It intentionally needs no system Python.
BUNDLE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
UV="$BUNDLE_DIR/uv"
[ -x "$UV" ] || { echo "KaroX portable runtime is incomplete: bundled uv is missing." >&2; exit 1; }

set -- "$BUNDLE_DIR"/karox_runtime-*.whl
[ "$#" -eq 1 ] && [ -f "$1" ] || { echo "KaroX portable runtime must contain exactly one wheel." >&2; exit 1; }
WHEEL=$1
WHEEL_NAME=$(basename -- "$WHEEL")

case "$(uname -s 2>/dev/null || true)" in
  Darwin)
    RUNTIME_DIR=${KAROX_RUNTIME_DIR:-"$HOME/.local/share/KaroX"}
    CONFIG_DIR=${KAROX_CONFIG_DIR:-"$HOME/Library/Application Support/KaroX"}
    ;;
  *)
    RUNTIME_DIR=${KAROX_RUNTIME_DIR:-"${XDG_DATA_HOME:-$HOME/.local/share}/KaroX"}
    CONFIG_DIR=${KAROX_CONFIG_DIR:-"${XDG_CONFIG_HOME:-$HOME/.config}/KaroX"}
    ;;
esac
VENV_DIR="$RUNTIME_DIR/.venv"
PYTHON="$VENV_DIR/bin/python"
MARKER="$RUNTIME_DIR/portable-wheel.txt"

mkdir -p "$RUNTIME_DIR" "$CONFIG_DIR" "$RUNTIME_DIR/uv-cache" "$RUNTIME_DIR/python"
export KAROX_RUNTIME_DIR="$RUNTIME_DIR"
export KAROX_CONFIG_DIR="$CONFIG_DIR"
export UV_CACHE_DIR="$RUNTIME_DIR/uv-cache"
export UV_PYTHON_INSTALL_DIR="$RUNTIME_DIR/python"

# Ambient project/user indexes must not redirect installation to an unrelated
# package source. Proxy and CA variables remain intact for corporate networks.
unset UV_INDEX UV_DEFAULT_INDEX UV_INDEX_URL UV_EXTRA_INDEX_URL UV_FIND_LINKS 2>/dev/null || true
unset PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_FIND_LINKS PIP_CONFIG_FILE 2>/dev/null || true
unset UV_PYTHON_INSTALL_MIRROR UV_PYTHON_CPYTHON_MIRROR UV_PYTHON_PYPY_MIRROR 2>/dev/null || true

if [ ! -x "$PYTHON" ]; then
  "$UV" --no-config venv --managed-python --python 3.12 "$VENV_DIR"
fi

INSTALLED=""
[ ! -f "$MARKER" ] || INSTALLED=$(cat "$MARKER" 2>/dev/null || true)
if [ "$INSTALLED" != "$WHEEL_NAME" ]; then
  "$UV" --no-config pip install --python "$PYTHON" --no-build --upgrade "$WHEEL"
  printf '%s\n' "$WHEEL_NAME" > "$MARKER"
fi

exec "$PYTHON" -m karox.cli "$@"
