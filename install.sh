#!/usr/bin/env bash
set -euo pipefail
installer="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.karox.sh"
[ -f "$installer" ] || { echo "install.karox.sh is missing." >&2; exit 1; }
exec bash "$installer" "$@"
