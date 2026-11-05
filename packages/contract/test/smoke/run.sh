#!/usr/bin/env bash
# Runs the smoke check against the packed @lsquare/contract, installed alone in a fresh directory,
# with the Node in PATH: through its ESM build, then its CommonJS one, each in a process of its own.
# No test runner: Vitest doesn't run on Node 20, which the contract supports.
# Usage: packages/contract/test/smoke/run.sh path/to/lsquare-contract-<version>.tgz
set -euo pipefail
tarball="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
here="$(cd "$(dirname "$0")" && pwd)"
dir="$(mktemp -d)"
trap 'rm -rf "${dir:?}"' EXIT
cp "$here/core.cjs" "$here/esm.mjs" "$here/cjs.cjs" "$dir/"
cd "$dir"
echo '{ "name": "lsquare-contract-smoke", "private": true }' > package.json
npm install --no-audit --no-fund --loglevel=error "$tarball"
echo "Node $(node --version): @lsquare/contract $(node -p "require('@lsquare/contract/package.json').version")"
failed=0
for file in esm.mjs cjs.cjs; do
  node "$file" || { echo "$file: exit code $?"; failed=1; }
done
exit "$failed"
