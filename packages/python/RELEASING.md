# Releasing `lsquare` for Python

`lsquare` goes to PyPI from GitHub, through [trusted publishing](https://docs.pypi.org/trusted-publishers/):
no token is stored anywhere. PyPI accepts an upload of `lsquare` only from the workflow
`pypi-release.yml` in `theagentexperiencecompany/lsquare`, which you run by hand from
`main`. Until launch, every release is a beta of 0.2.0 (`0.2.0b1`, `0.2.0b2`, …). PyPI has no
stable release of `lsquare`, so a plain `pip install lsquare` installs the newest beta.

## Who can release

Anyone with write access to the repository can start a release, but nothing is uploaded until the
`pypi` environment's required reviewer approves the run's `publish` job. PyPI checks the
repository, the workflow's file name and the environment, not the person or what the file says: a
job outside the environment is refused, and the environment takes only runs from `main`. The
workflow's own checks (from `main` only, and the rest below) stop mistakes; the approval stops
someone with write access acting alone. An admin can still change the environment, or bypass it
if it lets admins bypass its rules (setting up, below). Today write
access is the org's admins'; the org's members get read access by default, which can't start a
release.

So:

- Give the Write role (or higher) on this repository only to people you'd trust to publish, and
  the Admin role only to people you'd trust to change who approves.
- Approve only a run you started, from `main`, with the version you meant.
- Keep two-factor login on for everyone in the org: GitHub → the org's Settings → Authentication
  security → Require two-factor authentication.
- Keep PyPI to this one way in: delete any API token you don't use (PyPI → Account settings → API
  tokens).

## Setting up, once

1. **The environment,** before the first run: GitHub → the repository's Settings → Environments →
   New environment, named `pypi`. Under Deployment protection rules, tick Required reviewers and
   add yourself, and leave Prevent self-review unticked, since you approve your own runs. Untick
   Allow administrators to bypass configured protection rules, or an admin's run publishes without
   anyone's approval. Under Deployment branches and tags, choose Selected branches and tags, then
   Add deployment branch or tag rule: a branch, `main`. Save. A run that names an environment that
   doesn't exist makes it, with no reviewer, so make this one first.

The same publisher on TestPyPI, for rehearsals, and on PyPI. TestPyPI is a separate site, with its
own account and two-factor login.

| Field | Value |
|---|---|
| Owner | `theagentexperiencecompany` |
| Repository name | `lsquare` |
| Workflow name | `pypi-release.yml` |
| Environment name | `pypi` |

2. **TestPyPI:** test.pypi.org → Your projects → `lsquare` → Manage → Publishing → Add a new
   publisher → GitHub, with the values above. Then remove every other publisher listed there, such
   as one naming the repository's old owner or name.
3. **PyPI:** the same on pypi.org: Your projects → `lsquare` → Manage → Publishing.

## Each release

1. **The version:** a pull request sets `src/lsquare/_version.py` to the release's version
   (`0.2.0b1`) and says what changed in the repository's `CHANGELOG.md`, merged into `main` with CI
   green.
2. **The rehearsal:** GitHub → Actions → PyPI release → Run workflow, from `main`, with the
   version and `testpypi`. Or:

   ```bash
   gh workflow run pypi-release.yml --ref main -f version=0.2.0b1 -f index=testpypi
   ```

   Once CI and the checks pass, the run waits at `publish`: its page → Review deployments → `pypi`
   → Approve and deploy.
3. **The release:** the same, with `pypi`, and approved the same way.
4. **After it:** a pull request sets `_version.py` to the next beta's `.dev0` (`0.2.0b2.dev0`), so
   what's built in between doesn't say it's the release.

A run, in order:

- **preflight:** started from `main`; the version is a beta of 0.2.0 and the one `_version.py`
  says; the index doesn't have it yet (only its "not found" counts: an index that doesn't answer
  stops the release); and on PyPI, its tag `python-v<version>` isn't on another commit.
- **CI:** the whole of `ci.yml` on that commit: every Node and Python job, the built wheel tested
  on each Python version and each MCP SDK line. Its `python-build` job builds the wheel and sdist
  and checks them with pinned tools ([the build's pins](#the-builds-pins)), then records their
  SHA-256s; every Python job tests that artifact, taken by its ID, and those bytes.
- **the tested files:** CI's artifact, downloaded by the ID `python-build` gave it (never by its
  name), holds byte for byte the files whose SHA-256s it recorded: this version's wheel and sdist
  and nothing else, their metadata agreeing. Their SHA-256s go in the run's summary, beside its
  commit.
- **publish:** PyPA's action uploads those files, once they match those SHA-256s again. It's the
  only job that can publish; nothing else in the run can.
- **tag** (PyPI only): the commit gets the lightweight tag `python-v<version>`.
- **what the index serves:** once the index serves the version, its files are the tested ones byte
  for byte; `pip install lsquare==<version>` in a fresh venv installs it and `lsquare.mcp` imports;
  and on PyPI, a plain `pip install lsquare` installs it too.

No job of a run restores or saves anything in GitHub's cache, `ci.yml`'s included: the workflow
sets `cache-mode: none`, which GitHub's cache service enforces whatever a step runs. Caches are
written by runs a release doesn't vouch for, and restoring one unpacks an archive over the
runner's files; the weekly job (`mcp-sdk-releases.yml`), which runs SDK releases nobody has vetted
on `main`, gets no cache access either. So a release's CI downloads its dependencies afresh, and
takes a little longer than a pull request's.

Only one release runs at a time.

## When something fails

A version's files can never be replaced on PyPI or TestPyPI, not even after deleting them. So a
bad release is yanked (PyPI → `lsquare` → Manage → the release → Options → Yank), and the next
beta follows it.

- **Before `publish`:** nothing was uploaded. Fix what failed, and run again.
- **`publish` failed before uploading anything** (its log shows no file sent, and the index has no
  such version): run again.
- **`publish` failed partway** (the index has one file, the wheel or the sdist, but not both): before
  anything else is merged into `main`, run again with **recover** ticked and **commit** set to
  the commit the release ran on (the first line of its run's summary). The run builds that commit
  again (the build is reproducible: the same commit, with the same pinned backend), checks the
  files the index has are byte for byte this build's, and uploads only what's missing. It refuses
  if `main` has moved on since, since the missing file must come from the same commit as the one
  already up, and stops if the files differ: in either case, release the next beta instead.
- **After `publish`** (`tag` or the index checks failed): the release is out, and the version can't
  be published again. Don't run it again. A failed `tag` can be re-run (Re-run failed jobs keeps
  the run's commit), or made by hand:

  ```bash
  gh api repos/theagentexperiencecompany/lsquare/git/refs -f ref=refs/tags/python-v0.2.0b1 -f sha=<the run's commit>
  ```

  If the index serves files that aren't the tested ones, or the install fails, yank the version
  and release the next beta.
- **`the tested files` or `publish` finds files that aren't the ones `python-build` made**, or no
  artifact ID: nothing was uploaded, but something in the run replaced or changed the artifact, or
  the workflows' outputs are broken. Don't just run it again: read the run's logs and artifacts
  first.

## The build's pins

`build-constraints.txt` pins hatchling and everything it imports, and `twine-requirements.txt`
twine and everything it imports, each at an exact version with its hashes. CI's build and
`check_package.py`'s rebuild from the sdist take the first; `twine check` the second, installed
from wheels alone into a venv of its own. Neither file goes into the sdist, and someone building
the sdist gets `pyproject.toml`'s hatchling pin, not these.

Each file's first lines are the command that writes it. To move hatchling, change `requires` in
`pyproject.toml`'s `[build-system]` and run the command with the new version, in the same pull
request: `check_package_test.py` fails while the two disagree. Twine the same way. A recovery
builds the release's own commit, so it uses that commit's pins.

## Attestations

Each upload carries [PEP 740 attestations](https://docs.pypi.org/attestations/), PyPA's action's
default: PyPI shows each file's provenance, the repository, workflow and commit that built it. The
signing also writes those into Sigstore's public transparency log, for good, which is no more than
the public repository already shows.

## At launch

`0.2.0` itself needs the workflow to accept it: `RELEASABLE` in `scripts/release_check.py`. The
check that a plain `pip install lsquare` installs the release holds for it too.
