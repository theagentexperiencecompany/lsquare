#!/usr/bin/env bash
# Runs the smoke checks against the packed @lsquare/node, installed alone in a fresh directory, with
# the Node in PATH: each section of core.cjs in a process of its own, through the ESM build and then
# the CommonJS one. No test runner, which Vitest doesn't run on Node 18 or 20, and no
# unhandledRejection or uncaughtException listener: a failure there ends the process, as in an app.
# Usage: packages/node/test/smoke/run.sh path/to/lsquare-node-<version>.tgz
set -euo pipefail
tarball="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
here="$(cd "$(dirname "$0")" && pwd)"
dir="$(mktemp -d)"
trap 'rm -rf "${dir:?}"' EXIT
cp "$here/core.cjs" "$here/esm.mjs" "$here/cjs.cjs" "$dir/"
cd "$dir"
echo '{ "name": "lsquare-node-smoke", "private": true }' > package.json
npm install --no-audit --no-fund --loglevel=error "$tarball"
echo "Node $(node --version): @lsquare/node $(node -p "require('@lsquare/node/package.json').version")"
failed=0
for file in esm.mjs cjs.cjs; do
  for section in promises streams hooks; do
    node "$file" "$section" || { echo "$file $section: exit code $?"; failed=1; }
  done
done
exit "$failed"
