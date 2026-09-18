#!/usr/bin/env bash
set -euo pipefail

REPO_OWNER="kar0777"
REPO_NAME="KaroX"
REF="${KAROX_BOOTSTRAP_REF:-}"
RESOLVE_ONLY=0
CHANNEL="${KAROX_BOOTSTRAP_CHANNEL:-stable}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --resolve-only) RESOLVE_ONLY=1 ;;
    --channel)
      shift
      CHANNEL="${1:-}"
      ;;
    --channel=*) CHANNEL="${1#--channel=}" ;;
    *) echo "Unknown bootstrap argument: $1" >&2; exit 2 ;;
  esac
  shift
done
case "$CHANNEL" in stable|preview) ;; *) echo "Channel must be stable or preview." >&2; exit 2 ;; esac

if [ -z "$REF" ] && command -v curl >/dev/null 2>&1; then
  manifest="RELEASE.json"
  [ "$CHANNEL" != "preview" ] || manifest="PREVIEW.json"
  release_json="$(curl -fsSL --max-time 15 "https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/main/$manifest" 2>/dev/null || true)"
  REF="$(printf '%s' "$release_json" | sed -n 's/.*"tag"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
  if [ -z "$REF" ]; then
    version="$(printf '%s' "$release_json" | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
    if [ -n "$version" ]; then REF="v$version"; fi
  fi
fi
if [ -z "$REF" ]; then REF="main"; fi

if [ "$RESOLVE_ONLY" = 1 ]; then
  printf '%s\n' "$REF"
  exit 0
fi

INSTALL_ROOT="${KAROX_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/KaroX}"
SOURCE_DIR="$INSTALL_ROOT/source"
case "$REF" in v[0-9]*.[0-9]*.[0-9]*) REF_KIND="tags" ;; *) REF_KIND="heads" ;; esac

printf '\nKaroX %s installer\n----------------------------------------\n' "$CHANNEL"
printf 'Repository : https://github.com/%s/%s\nRelease/ref: %s\n\n' "$REPO_OWNER" "$REPO_NAME" "$REF"
command -v curl >/dev/null 2>&1 || { echo 'curl is required.' >&2; exit 1; }
command -v tar >/dev/null 2>&1 || { echo 'tar is required.' >&2; exit 1; }
mkdir -p "$INSTALL_ROOT"

portable_platform=""
case "$(uname -s 2>/dev/null || true):$(uname -m 2>/dev/null || true)" in
  Darwin:arm64|Darwin:aarch64) portable_platform="macos-arm64" ;;
  Darwin:x86_64) portable_platform="macos-x64" ;;
  Linux:aarch64|Linux:arm64) portable_platform="linux-arm64" ;;
  Linux:x86_64|Linux:amd64) portable_platform="linux-x64" ;;
esac

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  else return 1
  fi
}

if printf '%s' "$REF" | grep -Eq '^v5\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?$' && [ -n "$portable_platform" ]; then
  portable_asset="KaroX-${REF}-${portable_platform}-portable.tar.gz"
  checksum_asset="KaroX-${REF}-SHA256SUMS.txt"
  release_base="https://github.com/$REPO_OWNER/$REPO_NAME/releases/download/$REF"
  portable_archive="$(mktemp -t karox-portable-XXXXXX)"
  checksum_file="$(mktemp -t karox-checksums-XXXXXX)"
  if curl -fsSL "$release_base/$portable_asset" -o "$portable_archive" && curl -fsSL "$release_base/$checksum_asset" -o "$checksum_file"; then
    expected="$(awk -v asset="$portable_asset" '$2 == asset {print $1; exit}' "$checksum_file")"
    actual="$(sha256_file "$portable_archive" || true)"
    [ -n "$expected" ] || { echo 'Portable KaroX checksum entry is missing.' >&2; exit 1; }
    [ -n "$actual" ] || { echo 'sha256sum or shasum is required for portable KaroX.' >&2; exit 1; }
    if [ "$(printf '%s' "$actual" | tr 'A-F' 'a-f')" != "$(printf '%s' "$expected" | tr 'A-F' 'a-f')" ]; then
      echo 'Portable KaroX archive checksum mismatch; refusing to install.' >&2
      exit 1
    fi
    PORTABLE_DIR="$INSTALL_ROOT/portable"
    portable_stage="$INSTALL_ROOT/.portable-new-$$"
    portable_backup="$INSTALL_ROOT/.portable-old-$$"
    rm -rf "$portable_stage" "$portable_backup"
    mkdir -p "$portable_stage"
    if ! tar -xzf "$portable_archive" -C "$portable_stage" --strip-components=1; then
      rm -rf "$portable_stage"
      echo 'Portable KaroX archive could not be extracted.' >&2
      exit 1
    fi
    if [ ! -f "$portable_stage/karox" ] || [ ! -f "$portable_stage/uv" ]; then
      rm -rf "$portable_stage"
      echo 'Portable KaroX launcher or uv runtime is missing.' >&2
      exit 1
    fi
    chmod +x "$portable_stage/karox" "$portable_stage/uv"
    if [ -e "$PORTABLE_DIR" ]; then mv "$PORTABLE_DIR" "$portable_backup"; fi
    if mv "$portable_stage" "$PORTABLE_DIR"; then
      rm -rf "$portable_backup"
    else
      [ ! -e "$portable_backup" ] || mv "$portable_backup" "$PORTABLE_DIR"
      echo 'Portable KaroX update could not be activated; the previous install was restored.' >&2
      exit 1
    fi
    rm -f "$portable_archive" "$checksum_file"
    cat > "$INSTALL_ROOT/karox" <<EOF
#!/usr/bin/env sh
exec "$PORTABLE_DIR/karox" "\$@"
EOF
    chmod +x "$INSTALL_ROOT/karox"
    mkdir -p "$HOME/.local/bin"
    ln -sf "$INSTALL_ROOT/karox" "$HOME/.local/bin/karox"
    printf 'Portable KaroX installed: %s\n' "$PORTABLE_DIR"
    if [ "${KAROX_NO_START:-0}" = 1 ]; then exit 0; fi
    exec "$PORTABLE_DIR/karox"
  fi
  rm -f "$portable_archive" "$checksum_file"
  echo 'Portable asset was not available; falling back to the source installer.' >&2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
if [ -f "$SCRIPT_DIR/install.karox.sh" ] && [ -f "$SCRIPT_DIR/server/repo_tools.py" ]; then
  SOURCE_DIR="$SCRIPT_DIR"
else
  archive="$(mktemp -t karox-bootstrap-XXXXXX)"
  url="https://github.com/$REPO_OWNER/$REPO_NAME/archive/refs/$REF_KIND/$REF.tar.gz"
  rm -rf "$SOURCE_DIR"
  mkdir -p "$SOURCE_DIR"
  curl -fsSL "$url" -o "$archive"
  tar -xzf "$archive" -C "$SOURCE_DIR" --strip-components=1
  rm -f "$archive"
fi

[ -f "$SOURCE_DIR/install.karox.sh" ] || { echo 'install.karox.sh was not found.' >&2; exit 1; }
if [ "${KAROX_NO_START:-0}" = 1 ]; then
  exec bash "$SOURCE_DIR/install.karox.sh"
else
  exec bash "$SOURCE_DIR/install.karox.sh" --start
fi
