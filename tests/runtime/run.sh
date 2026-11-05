#!/usr/bin/env bash
# Runs one set of runtime tests against the packed package, installed in a fresh directory, with
# the Node in PATH: core (the package alone), sdk1 or sdk2 (with that MCP SDK's pinned version),
# types (its declarations, compiled under each module resolution by the TypeScript the lockfile has).
# Usage: tests/runtime/run.sh core|sdk1|sdk2|types path/to/lsquare-<version>.tgz
set -euo pipefail
suite=$1
tarball="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
here="$(cd "$(dirname "$0")" && pwd)"
pins="$here/../../packages/lsquare/package.json"
pin() { node -p "require('$pins').devDependencies['$1']"; }
# The exact version the lockfile installs, where the package's pin is a range.
lock="$here/../../package-lock.json"
locked() { node -p "require('$lock').packages['node_modules/$1'].version"; }
dir="$(mktemp -d)"
trap 'rm -rf "${dir:?}"' EXIT
cp "$here/ingestion.mjs" "$dir/"
case "$suite" in
  core) cp "$here"/core/*.test.mjs "$dir/"; with=() ;;
  sdk1) cp "$here/wrapper/sdk1.test.mjs" "$dir/"; with=("@modelcontextprotocol/sdk@$(pin @modelcontextprotocol/sdk)" "zod@$(pin zod)") ;;
  sdk2) cp "$here/wrapper/sdk2.test.mjs" "$dir/"; with=("@modelcontextprotocol/server@$(pin @modelcontextprotocol/server)" "@modelcontextprotocol/client@$(pin @modelcontextprotocol/client)" "zod@$(pin zod)") ;;
  types) cp -R "$here"/types/. "$dir/"; with=("typescript@$(locked typescript)" "@types/node@$(locked @types/node)" "@modelcontextprotocol/sdk@$(pin @modelcontextprotocol/sdk)" "@modelcontextprotocol/server@$(pin @modelcontextprotocol/server)" "zod@$(pin zod)") ;;
  *) echo "Unknown suite: $suite" >&2; exit 2 ;;
esac
cd "$dir"
echo '{ "name": "lsquare-runtime-test", "private": true, "type": "module" }' > package.json
npm install --no-audit --no-fund --loglevel=error "$tarball" ${with[@]+"${with[@]}"}
echo "Node $(node --version), $suite: lsquare $(node -p "require('lsquare/package.json').version")"
node --test
