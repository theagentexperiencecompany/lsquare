"""The release's checks (RELEASING.md), run by .github/workflows/pypi-release.yml. Each one reads the
run's inputs from the environment, never from an argument the shell has expanded: VERSION, INDEX
(`testpypi` or `pypi`), RECOVER (`true` to upload only what a release that failed partway is
missing), COMMIT (that release's commit) and, after CI, TESTED (the SHA-256 of each file CI's
python-build job made, as sha256sum writes them), with GitHub's own GITHUB_REF, GITHUB_SHA,
GITHUB_REPOSITORY and GH_TOKEN:

    python scripts/release_check.py preflight        # before CI: what may be released, and from where
    python scripts/release_check.py artifacts dist/  # after CI: the files python-build made, this version's
    python scripts/release_check.py published dist/  # after the upload: the index serves those bytes
    python scripts/release_check.py installs         # pip installs it from the index, in fresh venvs
    python scripts/release_check.py tag              # the commit tagged python-v<version>

Standard library only.
"""

from __future__ import annotations

import email.parser
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, NoReturn

NAME = "lsquare"
#: The only versions released until launch: 0.2.0's betas, written as PEP 440 normalizes them.
RELEASABLE = re.compile(r"0\.2\.0b[1-9][0-9]*")
BRANCH = "refs/heads/main"
#: Each index's JSON API and the simple index pip reads.
INDEXES = {
    "testpypi": ("https://test.pypi.org/pypi", "https://test.pypi.org/simple/"),
    "pypi": ("https://pypi.org/pypi", "https://pypi.org/simple/"),
}
ROOT = Path(__file__).resolve().parent.parent
#: How long an index may take to serve a new upload, through its JSON API and to pip.
PROPAGATION_S = 600
POLL_S = 15
#: Run with a fresh venv's Python, isolated (`-I`) in a temporary directory: the installed package.
INSTALLED = (
    "import importlib.metadata, json, lsquare, lsquare.mcp; "
    "print(json.dumps({'runtime': lsquare.__version__, 'distribution': importlib.metadata.version('lsquare'), 'file': lsquare.__file__}))"
)


def fail(message: str) -> NoReturn:
    print(f"::error::{message}")
    raise SystemExit(1)


def say(line: str) -> None:
    """A line in the log and in the run's summary."""
    print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as out:
            out.write(line + "\n")


class Unreachable(Exception):
    """The request got no answer the release can act on."""


def call(method: str, url: str, token: str | None = None, body: Any = None, answers: frozenset[int] = frozenset({404})) -> tuple[int, Any]:
    """The status and JSON of a request. A 2xx, or a status in `answers`, is an answer; anything else
    raises Unreachable, so a network error never reads as "not there"."""
    headers = {"User-Agent": "lsquare-release-check", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        if error.code in answers:
            return error.code, None
        raise Unreachable(f"{method} {url} answered {error.code}") from None
    # urllib turns only an OSError while sending into URLError; the status line and headers
    # (getresponse(), inside urlopen()) and the body (read()) raise http.client's own errors,
    # IncompleteRead, BadStatusLine and LineTooLong, which are HTTPException, not OSError.
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError) as error:
        raise Unreachable(f"{method} {url} failed: {error}") from None


def inputs() -> tuple[str, str, bool]:
    version = os.environ.get("VERSION", "")
    index = os.environ.get("INDEX", "")
    if index not in INDEXES:
        fail(f"INDEX is testpypi or pypi, not {index!r}")
    if RELEASABLE.fullmatch(version) is None:
        fail(f"{version!r} isn't a version this workflow releases: 0.2.0b1, 0.2.0b2 and so on")
    return version, index, os.environ.get("RECOVER", "") == "true"


def source_version() -> str:
    text = (ROOT / "src" / NAME / "_version.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    if match is None:
        fail("src/lsquare/_version.py holds no __version__")
    return match.group(1)


def held(index: str, version: str) -> dict[str, str] | None:
    """The files the index holds for `version`, each name with its SHA-256, or None for none."""
    status, body = call("GET", f"{INDEXES[index][0]}/{NAME}/{version}/json")
    if status == 404:
        return None
    try:
        return {str(item["filename"]): str(item["digests"]["sha256"]) for item in body["urls"]}
    except (KeyError, TypeError) as error:
        raise Unreachable(f"{index}'s answer for {version} has no file list: {error}") from None


def held_or_fail(index: str, version: str) -> dict[str, str] | None:
    try:
        return held(index, version)
    except Unreachable as error:
        fail(f"can't tell what {index} holds for {version}: {error}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def metadata(text: str, where: str, version: str) -> None:
    headers = email.parser.Parser().parsestr(text)
    if (headers.get("Name"), headers.get("Version")) != (NAME, version):
        fail(f"{where} names {headers.get('Name')} {headers.get('Version')}, not {NAME} {version}")


def built(dist: Path, version: str) -> dict[str, str]:
    """This version's wheel and sdist, and nothing else, each saying so in its metadata: names and
    SHA-256s. Hidden files (`uv build` writes a `.gitignore`) aren't uploaded: the upload takes
    `dist/*`."""
    wheel, sdist = f"{NAME}-{version}-py3-none-any.whl", f"{NAME}-{version}.tar.gz"
    found = sorted(path.name for path in dist.iterdir() if not path.name.startswith("."))
    if found != sorted((wheel, sdist)):
        fail(f"{dist} holds {found}, not {wheel} and {sdist}")
    with zipfile.ZipFile(dist / wheel) as archive:
        metadata(archive.read(f"{NAME}-{version}.dist-info/METADATA").decode("utf-8"), f"{wheel}'s METADATA", version)
    with tarfile.open(dist / sdist) as archive:
        member = archive.extractfile(f"{NAME}-{version}/PKG-INFO")
        if member is None:
            fail(f"{sdist} has no PKG-INFO")
        metadata(member.read().decode("utf-8"), f"{sdist}'s PKG-INFO", version)
    return {name: sha256(dist / name) for name in found}


def tested() -> dict[str, str]:
    """The SHA-256 of each file python-build made, recorded before anything but pinned code had run
    beside them: TESTED, a `<sha256>  <name>` line each, from ci.yml's outputs."""
    sums: dict[str, str] = {}
    for line in filter(None, os.environ.get("TESTED", "").splitlines()):
        match = re.fullmatch(r"([0-9a-f]{64}) [ *]([^/\\\s]+)", line)
        if match is None or match.group(2) in sums:
            fail(f"TESTED has a line that isn't another file's SHA-256: {line!r}")
        sums[match.group(2)] = match.group(1)
    if not sums:
        fail("TESTED is empty: the release takes only the files whose SHA-256s python-build recorded")
    return sums


def the_tested_files(dist: Path, version: str) -> dict[str, str]:
    """built(), and those files are byte for byte the ones python-build made, and all of them."""
    files, made = built(dist, version), tested()
    if files != made:
        fail(f"{dist} isn't what python-build made: it holds {files}, where python-build recorded {made}")
    return files


def tag_name(version: str) -> str:
    return f"python-v{version}"


def github() -> tuple[str, str, str, str]:
    """The API, repository, commit and token of this run."""
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    repo, sha, token = (os.environ.get(key, "") for key in ("GITHUB_REPOSITORY", "GITHUB_SHA", "GH_TOKEN"))
    if not (repo and sha and token):
        fail("GITHUB_REPOSITORY, GITHUB_SHA and GH_TOKEN must be set")
    return api, repo, sha, token


def tagged(version: str) -> str | None:
    """The commit the version's tag points at, or None without one. Only a lightweight tag is ours."""
    api, repo, _, token = github()
    try:
        status, body = call("GET", f"{api}/repos/{repo}/git/ref/tags/{tag_name(version)}", token)
    except Unreachable as error:
        fail(f"can't read the tag {tag_name(version)}: {error}")
    if status == 404:
        return None
    target = body.get("object", {}) if isinstance(body, dict) else {}
    if target.get("type") != "commit":
        fail(f"{tag_name(version)} isn't a lightweight tag on a commit: {target}")
    return str(target.get("sha"))


def preflight() -> None:
    version, index, recover = inputs()
    ref = os.environ.get("GITHUB_REF", "")
    if ref != BRANCH:
        fail(f"run this workflow from main, not {ref or 'an unknown ref'}")
    commit, sha = os.environ.get("COMMIT", ""), os.environ.get("GITHUB_SHA", "")
    if recover and commit != sha:
        # The files already up are that commit's build: what's missing must be too.
        fail(
            f"recovering builds the release's own commit, {commit or '(none given)'}, and main is at {sha}: "
            "if it has moved on since, release the next beta instead (RELEASING.md)"
        )
    if not recover and commit:
        fail("commit is only for recovering")
    source = source_version()
    if source != version:
        fail(f"src/lsquare/_version.py says {source}: set it to {version} in a pull request first")
    files = held_or_fail(index, version)
    if recover and files is None:
        fail(f"{index} has no files for {version}, so there's nothing to recover: run without recover")
    if not recover and files is not None:
        fail(
            f"{index} already has {version} ({', '.join(sorted(files))}). A version's files can never be "
            "replaced: release the next beta, or run with recover if this one failed partway (RELEASING.md)"
        )
    if index == "pypi":
        _, _, sha, _ = github()
        at = tagged(version)
        if at is not None and at != sha:
            fail(f"{tag_name(version)} already points at {at}, not this run's commit {sha}")
    say(f"Releasing {NAME} {version} to {index}{' (recovering)' if recover else ''} from {os.environ.get('GITHUB_SHA', '?')}.")


def artifacts(dist: Path) -> None:
    version, index, recover = inputs()
    files = the_tested_files(dist, version)
    present = held_or_fail(index, version)
    if not recover and present is not None:
        fail(f"{index} has had {version} since the preflight: {sorted(present)}")
    if recover:
        if present is None:
            fail(f"{index} has no files for {version}, so there's nothing to recover")
        for name, digest in sorted(present.items()):
            if files.get(name) != digest:
                fail(f"{index} holds {name} with SHA-256 {digest}, which this build doesn't make: release the next beta instead")
        if set(files) == set(present):
            fail(f"{index} already holds every file of {version}: nothing to recover")
        say(f"Missing from {index}: {', '.join(sorted(set(files) - set(present)))}.")
    say("| File | SHA-256 |\n|---|---|")
    for name, digest in files.items():
        say(f"| `{name}` | `{digest}` |")


def published(dist: Path) -> None:
    version, index, _ = inputs()
    files = the_tested_files(dist, version)
    deadline = time.monotonic() + PROPAGATION_S
    last: dict[str, str] | None | str = None
    while True:
        try:
            present = held(index, version)
            last = present
            if present is not None:
                for name, digest in sorted(present.items()):
                    if files.get(name) != digest:
                        fail(f"{index} serves {name} with SHA-256 {digest}, not the tested {files.get(name)}")
                if set(present) == set(files):
                    say(f"{index} serves {', '.join(sorted(files))}, byte for byte the files CI tested.")
                    return
        except Unreachable as error:
            last = str(error)
        if time.monotonic() >= deadline:
            fail(f"after {PROPAGATION_S} s, {index}'s answer for {version} is {last}, not {sorted(files)}")
        time.sleep(POLL_S)


def venv(root: Path, name: str) -> Path:
    place = root / name
    subprocess.run([sys.executable, "-m", "venv", str(place)], check=True)
    return place / ("Scripts" if os.name == "nt" else "bin") / "python"


def pip_install(python: Path, requirement: str, simple: str, until: float) -> None:
    """Installs from `simple` alone, retrying while the index catches up with the upload."""
    command = [str(python), "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check", "--index-url", simple, requirement]
    while True:
        done = subprocess.run(command, capture_output=True, text=True)
        if done.returncode == 0:
            return
        if time.monotonic() >= until:
            print(done.stdout + done.stderr)
            fail(f"pip couldn't install {requirement} from {simple}")
        time.sleep(POLL_S)


def isolated(python: Path, code: str, cwd: Path) -> str:
    done = subprocess.run([str(python), "-I", "-c", code], capture_output=True, text=True, cwd=cwd)
    if done.returncode != 0:
        print(done.stdout + done.stderr)
        fail(f"{python} couldn't run its check")
    return done.stdout.strip()


def installs() -> None:
    version, index, _ = inputs()
    simple = INDEXES[index][1]
    until = time.monotonic() + PROPAGATION_S
    with tempfile.TemporaryDirectory() as work:
        root = Path(work)
        pinned = venv(root, "pinned")
        pip_install(pinned, f"{NAME}=={version}", simple, until)
        found = json.loads(isolated(pinned, INSTALLED, root))
        if (found["runtime"], found["distribution"]) != (version, version):
            fail(f"{NAME}=={version} from {index} says {found}")
        if not Path(found["file"]).resolve().is_relative_to(root.resolve()):
            fail(f"the check imported {found['file']}, not the installed package")
        say(f"`pip install {NAME}=={version}` from {index} installs {version}, and `lsquare.mcp` imports.")
        if index == "pypi":
            # PyPI has no stable release before launch, so pip takes the newest beta: this one. At
            # launch, 0.2.0 is the newest stable release, and so this one too.
            plain = venv(root, "plain")
            pip_install(plain, NAME, simple, until)
            got = isolated(plain, "import importlib.metadata; print(importlib.metadata.version('lsquare'))", root)
            if got != version:
                fail(f"a plain `pip install {NAME}` installs {got}, not {version}")
            say(f"A plain `pip install {NAME}` installs {got}.")


def tag() -> None:
    version, index, _ = inputs()
    if index != "pypi":
        fail("only a release to PyPI is tagged")
    api, repo, sha, token = github()
    name = tag_name(version)
    try:
        status, _ = call("POST", f"{api}/repos/{repo}/git/refs", token, {"ref": f"refs/tags/{name}", "sha": sha}, frozenset({422}))
    except Unreachable as error:
        fail(f"couldn't tag {sha} {name}: {error}")
    if status == 422:
        # Already there: from an earlier run of this release, or it was made by hand.
        at = tagged(version)
        if at != sha:
            fail(f"{name} points at {at}, not {sha}; tags are never moved")
        say(f"{name} already points at {sha}.")
        return
    say(f"Tagged {sha} {name}.")


def main(argv: list[str]) -> None:
    steps = {"preflight": preflight, "installs": installs, "tag": tag}
    with_dist = {"artifacts": artifacts, "published": published}
    if len(argv) == 1 and argv[0] in steps:
        steps[argv[0]]()
    elif len(argv) == 2 and argv[0] in with_dist:
        with_dist[argv[0]](Path(argv[1]))
    else:
        fail(f"usage: release_check.py {'|'.join(steps)}, or {'|'.join(with_dist)} <dist>")


if __name__ == "__main__":
    main(sys.argv[1:])
