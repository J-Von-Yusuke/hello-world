#!/usr/bin/env bash
# Install CacheLib v20240621 + RU/stats/Nemo patch on an Ubuntu machine.
#
#   ./install.sh [TARGET_DIR]           fresh clone + patch + full build
#   ./install.sh --existing DIR         patch an existing v20240621 checkout
#                                       and rebuild only cachebench
#
# Tested on Ubuntu 24.04 (WSL2). 22.04 also passes build.sh's OS check.
# The full dependency build (folly, fbthrift, ...) takes 30-90 min.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PATCH="$HERE/cachelib-v20240621-ru-stats.patch"
TAG=v20240621
COMMIT=c5c0d9b

die() { echo "ERROR: $*" >&2; exit 1; }
[ -f "$PATCH" ] || die "patch not found: $PATCH"

apply_patch() { # $1 = repo dir
  cd "$1"
  local head
  head=$(git rev-parse --short=7 HEAD)
  [ "$head" = "$COMMIT" ] || die "$1 is at $head, expected $TAG ($COMMIT)"
  if git apply --reverse --check "$PATCH" 2>/dev/null; then
    echo "patch already applied"
  else
    git apply --check "$PATCH" || die "patch does not apply cleanly"
    git apply "$PATCH"
    echo "patch applied"
  fi
}

if [ "${1:-}" = "--existing" ]; then
  DIR="${2:?usage: $0 --existing DIR}"
  apply_patch "$DIR"
  [ -d "$DIR/build-cachelib" ] || die "no build-cachelib in $DIR (run a full build first)"
  cd "$DIR/build-cachelib"
  cmake . >/dev/null          # new source files (NemoCache.cpp)
  make -j"$(nproc)" cachebench
  echo "OK: $DIR/build-cachelib/cachebench/cachebench"
  exit 0
fi

DIR="${1:-$HOME/cachelib-$TAG-ru}"
command -v git >/dev/null || sudo apt-get install -y git
if [ ! -d "$DIR/.git" ]; then
  git clone --branch "$TAG" https://github.com/facebook/CacheLib.git "$DIR"
fi
cd "$DIR"
# full-history submodules: build.sh checks out pinned revisions
git submodule update --init
git submodule foreach "git fetch --unshallow -q 2>/dev/null || true" >/dev/null
apply_patch "$DIR"
cd "$DIR"
./contrib/build.sh -j -T
echo "OK: $DIR/build-cachelib/cachebench/cachebench"
echo "run with: LD_LIBRARY_PATH=$DIR/opt/cachelib/lib:$DIR/opt/cachelib/lib64"
