#!/usr/bin/env sh
# Install or update KaroX 6 from its git branch into a venv, without a working KaroX.
#
# Idempotent: creates the venv when it is missing, does nothing when the installed build
# is already the branch's newest commit, and otherwise builds the branch in a temporary
# clone, checks the wheel and installs it. The same steps as `karox6 update`.
#
#   sh scripts/install_latest.sh [--venv DIR] [--repo URL] [--branch NAME] [--force]
set -eu

VENV="${KAROX_VENV:-$HOME/karox6-venv}"
REPO="${KAROX_UPDATE_REPO:-https://github.com/kar0777/KaroX.git}"
BRANCH="${KAROX_UPDATE_BRANCH:-karox6/main}"
PYTHON="${PYTHON:-python3}"
FORCE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --venv) VENV="$2"; shift 2 ;;
        --repo) REPO="$2"; shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        --python) PYTHON="$2"; shift 2 ;;
        --force) FORCE=1; shift ;;
        *) echo "install_latest: unknown option $1" >&2; exit 2 ;;
    esac
done

fail() { echo "install_latest: $1" >&2; exit 1; }

command -v git >/dev/null 2>&1 || fail "git is not installed or not on PATH"
PY="$VENV/bin/python"
if [ ! -x "$PY" ]; then
    echo "creating the venv $VENV ..."
    "$PYTHON" -m venv "$VENV" || fail "cannot create the venv $VENV (Python 3.12+ needed)"
fi

HEAD=$(git ls-remote "$REPO" "refs/heads/$BRANCH" | awk 'NR==1 {print $1}')
[ -n "$HEAD" ] || fail "cannot read $BRANCH from $REPO"
BEFORE=$("$PY" -c 'import karox6
try:
    from karox6 import _build as b; c = b.COMMIT
except Exception:
    c = ""
print(karox6.__version__, c)' 2>/dev/null || true)
OLD_VERSION=$(echo "$BEFORE" | awk '{print $1}')
OLD_COMMIT=$(echo "$BEFORE" | awk '{print $2}')
[ -n "$OLD_VERSION" ] || OLD_VERSION=none
if [ "$FORCE" = 0 ] && [ "$OLD_COMMIT" = "$HEAD" ]; then
    echo "KaroX $OLD_VERSION ($(echo "$HEAD" | cut -c1-7)) is up to date."
    exit 0
fi

TEMP=$(mktemp -d "${TMPDIR:-/tmp}/karox6-install-XXXXXX")
trap 'rm -rf "$TEMP"' EXIT INT TERM
CLONE="$TEMP/repo"
echo "fetching $BRANCH from $REPO ..."
git clone --quiet --filter=blob:none --single-branch --branch "$BRANCH" "$REPO" "$CLONE" \
    || fail "cannot clone $BRANCH from $REPO"
COMMIT=$(git -C "$CLONE" rev-parse HEAD)
# Python repr() of the values (what `karox6 update` does): a repo or branch with a '
# or a trailing \ must not produce an invalid _build.py and a build that fails.
"$PY" - "$CLONE/src/karox6/_build.py" "$REPO" "$BRANCH" "$COMMIT" <<'EOF' || fail "cannot write _build.py"
import sys
with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as f:
    f.write('"""Written by install_latest.sh: where this build came from."""\n\n')
    for name, value in zip(("REPO", "BRANCH", "COMMIT"), sys.argv[2:]):
        f.write("{} = {!r}\n".format(name, value))
EOF

echo "building ..."
"$PY" -m pip wheel --disable-pip-version-check --no-deps --quiet --wheel-dir "$TEMP/dist" "$CLONE" \
    || fail "building the wheel failed (the installed KaroX was kept)"
WHEEL=$(ls "$TEMP"/dist/karox6-*.whl | head -n 1)

echo "checking the new build ..."
"$PY" -m pip install --disable-pip-version-check --no-deps --no-compile --quiet \
    --target "$TEMP/check" "$WHEEL" || fail "unpacking the new build failed"
"$PY" -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import karox6.cli as c; raise SystemExit(c.main(["--version"]))' \
    "$TEMP/check" >/dev/null || fail "the new build does not start (the installed KaroX was kept)"

echo "installing ..."
"$PY" -m pip install --disable-pip-version-check --quiet "$WHEEL" \
    || fail "installing the dependencies failed (the installed KaroX was kept)"
"$PY" -m pip install --disable-pip-version-check --quiet --force-reinstall --no-deps "$WHEEL" \
    || fail "installing the new build failed"
AFTER=$("$VENV/bin/karox6" --version) || fail "karox6 --version fails after the install"
echo "KaroX: $OLD_VERSION -> ${AFTER#karox } ($(echo "$COMMIT" | cut -c1-7))"
if [ -n "$OLD_COMMIT" ]; then
    git -C "$CLONE" log --no-merges --format='  %h %s' -20 "$OLD_COMMIT..HEAD" 2>/dev/null || true
fi
