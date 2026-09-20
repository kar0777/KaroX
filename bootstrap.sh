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

# All network reads are HTTPS-only, size- and time-bounded. A 404 is the
# only download result that can mean an optional portable asset is absent.
download() {
  local url="$1" output="$2" limit="$3" status code location origin
  local started=$SECONDS remaining redirect
  for redirect in 0 1 2 3 4 5; do
    case "$url" in
      https://github.com/*|https://raw.githubusercontent.com/*|https://codeload.github.com/*|https://release-assets.githubusercontent.com/*) ;;
      *) echo 'KaroX download refused: untrusted HTTPS source/redirect.' >&2; return 1 ;;
    esac
    remaining=$((120 - (SECONDS - started)))
    [ "$remaining" -gt 0 ] || { echo 'KaroX download timed out.' >&2; return 1; }
    code=0
    status="$( (ulimit -f "$((limit / 1024 + 1))"; curl --proto '=https' --proto-redir '=https' \
      -sS --connect-timeout 15 --max-time "$remaining" --max-filesize "$limit" \
      --dump-header "$output.headers" -w '%{http_code}' "$url" -o "$output") )" || code=$?
    if [ "$code" -ne 0 ]; then
      echo "KaroX download failed (curl $code): check network/TLS, disk space, and retry." >&2
      return 1
    fi
    case "$status" in
      301|302|303|307|308)
        location="$(awk 'tolower($1) == "location:" {sub(/^[^:]*:[ \t]*/, ""); sub(/\r$/, ""); value=$0} END {print value}' "$output.headers")"
        origin="${url#https://}"; origin="https://${origin%%/*}"
        case "$location" in
          https://*) url="$location" ;;
          //*) url="https:$location" ;;
          /*) url="$origin$location" ;;
          *) echo 'KaroX download refused: unsupported redirect.' >&2; return 1 ;;
        esac
        continue ;;
    esac
    if [ "$status" = 404 ]; then return 44; fi
    if [ "$status" != 200 ] || [ "$(wc -c < "$output" | tr -d ' ')" -gt "$limit" ]; then
      echo "KaroX download refused (HTTP $status or size limit exceeded)." >&2
      return 1
    fi
    return 0
  done
  echo 'KaroX download exceeded the redirect limit.' >&2
  return 1
}

command -v curl >/dev/null 2>&1 || { echo 'curl is required.' >&2; exit 1; }
work="$(mktemp -d "${TMPDIR:-/tmp}/karox-bootstrap.XXXXXX")"
trap 'rm -rf "$work"' EXIT
if [ -z "$REF" ]; then
  manifest="RELEASE.json"
  [ "$CHANNEL" != "preview" ] || manifest="PREVIEW.json"
  download "https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/main/$manifest" "$work/release.json" 65536 || {
    echo "Cannot resolve $CHANNEL release; no unverified branch fallback. Retry or set KAROX_BOOTSTRAP_REF." >&2; exit 1;
  }
  release_json="$(cat "$work/release.json")"
  REF="$(printf '%s' "$release_json" | sed -n 's/.*"tag"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
  if [ -z "$REF" ]; then
    version="$(printf '%s' "$release_json" | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
    if [ -n "$version" ]; then REF="v$version"; fi
  fi
fi
if [ -z "$REF" ]; then echo 'Release manifest has no tag or version.' >&2; exit 1; fi
if ! printf '%s' "$REF" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._/-]*$' || [[ "$REF" == *..* ]]; then
  echo 'Invalid bootstrap ref.' >&2; exit 2
fi

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
  portable_archive="$work/portable.tar.gz"
  checksum_file="$work/SHA256SUMS.txt"
  portable_result=0
  download "$release_base/$portable_asset" "$portable_archive" 268435456 || portable_result=$?
  if [ "$portable_result" = 0 ]; then
    download "$release_base/$checksum_asset" "$checksum_file" 1048576 || {
      echo 'Portable checksums could not be downloaded; refusing source fallback.' >&2; exit 1;
    }
    expected="$(awk -v asset="$portable_asset" '$2 == asset {print $1; exit}' "$checksum_file")"
    actual="$(sha256_file "$portable_archive" || true)"
    printf '%s' "$expected" | grep -Eq '^[0-9a-fA-F]{64}$' || { echo 'Portable KaroX checksum entry is missing or invalid.' >&2; exit 1; }
    [ -n "$actual" ] || { echo 'sha256sum or shasum is required for portable KaroX.' >&2; exit 1; }
    if [ "$(printf '%s' "$actual" | tr 'A-F' 'a-f')" != "$(printf '%s' "$expected" | tr 'A-F' 'a-f')" ]; then
      echo 'Portable KaroX archive checksum mismatch; refusing to install.' >&2
      exit 1
    fi
    PORTABLE_DIR="$INSTALL_ROOT/portable"
    portable_stage="$(mktemp -d "$INSTALL_ROOT/.portable-new.XXXXXX")"
    portable_backup="${portable_stage}.previous"
    # Release bundles are flat. Validate names, uniqueness, member types and
    # count before extraction; reject links/devices, traversal and nested paths.
    portable_root="${portable_asset%.tar.gz}"
    if ! tar -tzf "$portable_archive" | awk -v root="$portable_root" '
      ++count > 16 {exit 1}
      $0 == root "/" {next}
      index($0, root "/") != 1 {exit 1}
      {name=substr($0, length(root)+2)}
      name !~ /^[A-Za-z0-9][A-Za-z0-9._+-]*$/ || name == "." || name == ".." || seen[name]++ {exit 1}
      END {if (!count) exit 1}' ||
      ! LC_ALL=C tar -tvzf "$portable_archive" | awk 'substr($0,1,1) !~ /[-d]/ {exit 1}'; then
      rm -rf "$portable_stage"
      echo 'Portable KaroX archive has unsafe paths, links, or layout.' >&2; exit 1
    fi
    if ! (umask 077; ulimit -f 524288; tar -xzf "$portable_archive" -C "$portable_stage" --strip-components=1 --no-same-owner --no-same-permissions); then
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
    # Quote the install path as shell data, including dollar signs/backticks.
    { printf '%s\n' '#!/usr/bin/env bash'; printf 'exec %q "$@"\n' "$PORTABLE_DIR/karox"; } > "$INSTALL_ROOT/karox"
    chmod +x "$INSTALL_ROOT/karox"
    mkdir -p "$HOME/.local/bin"
    ln -sf "$INSTALL_ROOT/karox" "$HOME/.local/bin/karox"
    printf 'Portable KaroX installed: %s\n' "$PORTABLE_DIR"
    printf 'Run now: "%s/karox"\n' "$HOME/.local/bin"
    printf 'If karox is not found, run: export PATH="$HOME/.local/bin:$PATH"\n'
    if [ "${KAROX_NO_START:-0}" = 1 ]; then exit 0; fi
    rm -rf "$work"; trap - EXIT
    exec "$PORTABLE_DIR/karox"
  fi
  if [ "$portable_result" != 44 ]; then exit 1; fi
  rm -f "$portable_archive" "$checksum_file"
  echo 'Portable asset was not available; falling back to the source installer.' >&2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
if [ -f "$SCRIPT_DIR/install.karox.sh" ] && [ -f "$SCRIPT_DIR/server/repo_tools.py" ]; then
  SOURCE_DIR="$SCRIPT_DIR"
else
  # Source archives have no release checksum manifest. Only an explicitly
  # supplied digest authorizes this developer fallback; never execute unchecked
  # source just because the portable download failed.
  if ! printf '%s' "${KAROX_SOURCE_SHA256:-}" | grep -Eq '^[0-9a-fA-F]{64}$'; then
    echo 'No portable bundle for this ref/platform. Use a source checkout installer, or set KAROX_SOURCE_SHA256 to the trusted source archive digest and retry.' >&2
    exit 1
  fi
  archive="$work/source.tar.gz"
  url="https://github.com/$REPO_OWNER/$REPO_NAME/archive/refs/$REF_KIND/$REF.tar.gz"
  if printf '%s' "$REF" | grep -Eq '^[0-9a-fA-F]{40}$'; then
    url="https://github.com/$REPO_OWNER/$REPO_NAME/archive/$REF.tar.gz"
  fi
  download "$url" "$archive" 268435456 || exit 1
  actual="$(sha256_file "$archive")"
  if [ "$(printf '%s' "$actual" | tr 'A-F' 'a-f')" != "$(printf '%s' "$KAROX_SOURCE_SHA256" | tr 'A-F' 'a-f')" ]; then
    echo 'Source archive checksum mismatch; refusing to install.' >&2; exit 1
  fi
  # Developer fallback requires Python, unlike the primary portable flow.
  command -v python3 >/dev/null 2>&1 || { echo 'Source fallback requires Python 3; use a portable release on a clean machine.' >&2; exit 1; }
  source_stage="$(mktemp -d "$INSTALL_ROOT/.source-new.XXXXXX")"
  if ! python3 - "$archive" "$source_stage" <<'PYSAFE'
import pathlib, shutil, sys, tarfile
root = pathlib.Path(sys.argv[2])
with tarfile.open(sys.argv[1], "r:gz") as bundle:
    entries = bundle.getmembers()
    if len(entries) > 20000 or sum(m.size for m in entries) > 1024**3:
        raise SystemExit("Source archive exceeds extraction limits")
    prefix = None
    seen = set()
    for m in entries:
        p = pathlib.PurePosixPath(m.name)
        if p.is_absolute() or ".." in p.parts or "\\" in m.name or not (m.isfile() or m.isdir()):
            raise SystemExit("Unsafe source archive member")
        if not p.parts or (prefix is not None and p.parts[0] != prefix):
            raise SystemExit("Unexpected source archive layout")
        prefix = p.parts[0]
        target = root.joinpath(*p.parts[1:])
        if target in seen:
            raise SystemExit("Duplicate source archive member")
        seen.add(target)
        if m.isdir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(m) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, 65536)
            target.chmod(0o700 if m.mode & 0o111 else 0o600)
PYSAFE
  then
    rm -rf "$source_stage"; echo 'Source archive extraction refused.' >&2; exit 1
  fi
  [ -f "$source_stage/install.karox.sh" ] || { rm -rf "$source_stage"; echo 'Source installer missing.' >&2; exit 1; }
  source_backup="${source_stage}.previous"
  if [ -e "$SOURCE_DIR" ]; then mv "$SOURCE_DIR" "$source_backup"; fi
  if ! mv "$source_stage" "$SOURCE_DIR"; then
    [ ! -e "$source_backup" ] || mv "$source_backup" "$SOURCE_DIR"
    echo 'Source update failed; previous source restored.' >&2; exit 1
  fi
  rm -rf "$source_backup"
fi

[ -f "$SOURCE_DIR/install.karox.sh" ] || { echo 'install.karox.sh was not found.' >&2; exit 1; }
rm -rf "$work"; trap - EXIT
if [ "${KAROX_NO_START:-0}" = 1 ]; then
  exec bash "$SOURCE_DIR/install.karox.sh"
else
  exec bash "$SOURCE_DIR/install.karox.sh" --start
fi
