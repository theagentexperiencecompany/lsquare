# Releasing `lsquare` for npm

`lsquare` goes to npm by hand, and only as the tarball CI built and tested for a commit of
`main`: the `lsquare-package` artifact of that commit's push run, the file its core, wrapper and
types jobs installed and tested. Never a folder: `npm publish` in `packages/lsquare` (or with
`-w lsquare`) stops at `prepublishOnly`, which refuses, while a tarball's publish doesn't run it.
The refusal stops mistakes, not intent: `--ignore-scripts` gets past it.

Every version goes out under the `next` tag (`publishConfig.tag`); `latest` moves to it once it's
out, by hand (step 7). Both have pointed at 0.2.0 since 2 November 2026. An owner of `lsquare` on npm publishes (`npm owner ls lsquare`).
`@lsquare/node` (`packages/node`, 0.1) still publishes from its folder.

## Each release

From a checkout of the repository, with `gh` signed in:

1. **The version:** a pull request sets `version` in `packages/lsquare/package.json`, its entry in
   the root `package-lock.json`, and `SDK_VERSION` in `src/client.ts`, which events send, with the
   tests that expect it, and says what changed in `CHANGELOG.md`. It's merged into `main` with CI
   green.
2. **The commit's CI:** the push run of `ci.yml` for that exact commit of `main`, finished and
   green. A pull request's run doesn't count: its tarball is built from a merge commit, and one
   that changes Markdown alone skips the tests.

   ```bash
   git fetch origin main
   commit=$(git rev-parse origin/main)
   gh run list --workflow ci.yml --branch main --event push --commit "$commit" --json databaseId,status,conclusion
   ```

   Its `status` is `completed` and its `conclusion` `success`; `databaseId` is the run's ID.
3. **Its tarball,** into an empty directory that `mktemp` makes. npm keeps the tarball's path in
   the version's public metadata for good (`_from` and `_resolved`), and `mktemp`'s path names no
   one; a folder under your home or a tool's working folder would.

   ```bash
   dir=$(mktemp -d)
   gh run download <run ID> --name lsquare-package --dir "$dir"
   ls -A "$dir"
   ```

   It holds `lsquare-<version>.tgz` and nothing else. CI keeps the artifact for 7 days
   (`retention-days` in `ci.yml`): after that, re-run the whole run (`gh run rerun <run ID>`,
   which GitHub allows for 30 days after it) and download again, or, later still, merge a change
   into `main` and take that commit's push run.
4. **What it holds:**

   ```bash
   tar -xzOf "$dir/lsquare-<version>.tgz" package/package.json | node -e 'const p = JSON.parse(require("fs").readFileSync(0, "utf8")); console.log(p.name, p.version, p.publishConfig.tag)'
   npm view lsquare@<version> version
   node -e 'console.log("sha512-" + require("crypto").createHash("sha512").update(require("fs").readFileSync(process.argv[1])).digest("base64"))' "$dir/lsquare-<version>.tgz"
   ```

   The first prints `lsquare <version> next`; the second answers `E404`, since npm doesn't have
   the version yet; the third is the tarball's integrity, for step 6.
5. **The release,** that file, with two-factor authentication:

   ```bash
   npm publish "$dir/lsquare-<version>.tgz" --tag next
   ```

   `--tag next` repeats the tarball's `publishConfig`: npm's notice names only a tag given on the
   command line, and says `latest` without one.
6. **After it:** npm serves those bytes, under `next`:

   ```bash
   npm view lsquare@<version> dist.integrity
   npm view lsquare dist-tags
   npm view lsquare@<version> _from _resolved
   ```

   The integrity is step 4's, the tags are `next: <version>` and, until step 7, `latest` the
   version before, and the paths are step 3's directory.
7. **`latest`,** for a version without a prerelease part (`0.2.1`, not `0.3.0-beta.1`):
   `npm dist-tag add lsquare@<version> latest`, so a plain `npm install lsquare` gets it too.

## When something fails

- **Before `npm publish`, or a publish that failed** (npm doesn't have the version): nothing is
  out. Fix what failed and start again from that step.
- **After it**, if npm's integrity isn't the tarball's, or the release is bad: a version number is
  never used twice on npm, even unpublished. Deprecate it
  (`npm deprecate lsquare@<version> "<why>"`), move `latest` back if step 7 moved it
  (`npm dist-tag add lsquare@<previous version> latest`), and release the next one.

## Publishing to `latest` directly

To skip step 7, change `publishConfig.tag` in `package.json` and the `--tag` above in the same
pull request.
