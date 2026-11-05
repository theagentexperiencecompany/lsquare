"""Checks the built wheel and sdist as they would be published (spec section 7), as
packages/lsquare/scripts/check-package.mjs does for npm:

    python scripts/check_package.py dist/

- each holds exactly the files listed below, nothing else: the package, its metadata, and in the
  sdist what a build needs (never the tests, the scripts or RELEASING.md);
- no file of either matches the denylist: infrastructure IDs, personal paths, email addresses and
  credential formats. A denylist only finds what it lists; it complements reading the package, it
  doesn't replace it;
- the wheel's metadata names no runtime dependency and Python 3.10 or newer;
- a wheel built from the sdist, with the backend build-constraints.txt pins, holds the same files,
  byte for byte, as the wheel built from the tree.

Standard library only; `twine check` runs after it in CI, from twine-requirements.txt.
"""

from __future__ import annotations

import email.parser
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

#: hatchling and everything it imports, at the versions and hashes CI's build uses: the
#: repository's file, which the sdist doesn't hold.
BUILD_CONSTRAINTS = Path(__file__).resolve().parent.parent / "build-constraints.txt"

#: Every file of the package, as the wheel holds them, and the sdist under src/. A file added to the
#: package is added here too, so nothing ships unseen.
PACKAGE = (
    "lsquare/__init__.py",
    "lsquare/_client.py",
    "lsquare/_event.py",
    "lsquare/_jsjson.py",
    "lsquare/_normalize.py",
    "lsquare/_redact.py",
    "lsquare/_runtime.py",
    "lsquare/_secret_spans.py",
    "lsquare/_sending.py",
    "lsquare/_shape.py",
    "lsquare/_threads.py",
    "lsquare/_types.py",
    "lsquare/_url.py",
    "lsquare/_values.py",
    "lsquare/_version.py",
    "lsquare/mcp/__init__.py",
    "lsquare/mcp/_boundary.py",
    "lsquare/mcp/_call.py",
    "lsquare/mcp/_extras.py",
    "lsquare/mcp/_fastmcp.py",
    "lsquare/mcp/_lists.py",
    "lsquare/mcp/_ownership.py",
    "lsquare/mcp/_read.py",
    "lsquare/mcp/_registry.py",
    "lsquare/mcp/_run.py",
    "lsquare/mcp/_v1.py",
    "lsquare/mcp/_v2.py",
    "lsquare/mcp/_weak.py",
    "lsquare/py.typed",
)
LICENCES = ("LICENSE", "THIRD_PARTY_NOTICES")
#: The wheel's metadata, in its lsquare-<version>.dist-info/.
DIST_INFO = ("METADATA", "WHEEL", "RECORD", *(f"licenses/{licence}" for licence in LICENCES))
#: The sdist's files besides src/: hatchling adds PKG-INFO, and .gitignore (ordinary build and cache
#: exclusions), whatever the build's include list says.
SDIST = ("PKG-INFO", "pyproject.toml", ".gitignore", "README.md", "REFERENCE.md", *LICENCES)

FORBIDDEN = tuple(
    re.compile(pattern, re.ASCII | flags)
    for pattern, flags in (
        # Infrastructure IDs, such as Cloudflare's.
        (r"\b[0-9a-f]{32}\b", 0),
        # Personal details.
        (r"/Users/|/home/", 0),
        (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", 0),
        # Credentials. The redaction patterns are regexes, or prefixes alone, so a real value never
        # matches them by accident: a real Slack webhook has its IDs after the prefix.
        (r"lsq_[A-Za-z0-9_-]{20,}", 0),
        (r"\bsk[-_](?:live_)?[A-Za-z0-9]{16,}", 0),
        (r"xox[abprs]-[A-Za-z0-9-]{10,}", 0),
        (r"hooks\.slack\.com/services/[A-Z0-9]{8,}", 0),
        (r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}", 0),
        (r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}", 0),
        (r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.", 0),
        (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", 0),
        (r"Bearer\s+[A-Za-z0-9._~+/-]{16,}", 0),
    )
)


def fail(message: str) -> None:
    print(f"FAIL {message}")
    raise SystemExit(1)


def one(directory: Path, pattern: str) -> Path:
    found = sorted(directory.glob(pattern))
    if len(found) != 1:
        fail(f"expected one {pattern} in {directory}, found {[path.name for path in found]}")
    return found[0]


def wheel_files(wheel: Path) -> dict[str, bytes]:
    """Its files. A name held twice is refused: only one of the two would be checked."""
    with zipfile.ZipFile(wheel) as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        names = [info.filename for info in infos]
        if len(set(names)) != len(names):
            fail(f"{wheel.name} holds some names twice: {sorted({name for name in names if names.count(name) > 1})}")
        return {info.filename: archive.read(info) for info in infos}


def sdist_files(sdist: Path) -> dict[str, bytes]:
    """Its files, without their one root directory. A name held twice, a second root, or anything
    but a file or a directory is refused."""
    files: dict[str, bytes] = {}
    with tarfile.open(sdist) as archive:
        members = archive.getmembers()
        roots = {member.name.split("/", 1)[0] for member in members}
        if len(roots) != 1:
            fail(f"{sdist.name} has {len(roots)} roots: {sorted(roots)}")
        for member in members:
            if member.isdir():
                continue
            if not member.isfile():
                fail(f"{sdist.name} holds {member.name}, which isn't a file")
            name = member.name.split("/", 1)[1] if "/" in member.name else member.name
            if name in files:
                fail(f"{sdist.name} holds {member.name} twice")
            data = archive.extractfile(member)
            files[name] = data.read() if data else b""
    return files


def unexpected(files: dict[str, bytes], expected: set[str]) -> list[str]:
    problems = []
    if extra := sorted(set(files) - expected):
        problems.append(f"unexpected files: {extra}")
    if missing := sorted(expected - set(files)):
        problems.append(f"missing files: {missing}")
    return problems


def forbidden(files: dict[str, bytes]) -> list[str]:
    return [
        f"{name} matches {pattern.pattern}"
        for name, data in sorted(files.items())
        for pattern in FORBIDDEN
        if pattern.search(data.decode("utf-8", "replace"))
    ]


def wheel_problems(files: dict[str, bytes]) -> list[str]:
    infos = {name.split("/", 1)[0] for name in files if ".dist-info/" in name}
    if len(infos) != 1:
        return [f"expected one .dist-info directory, found {sorted(infos)}"]
    [info] = infos
    problems = unexpected(files, {*PACKAGE, *(f"{info}/{name}" for name in DIST_INFO)}) + forbidden(files)
    metadata = email.parser.BytesParser().parsebytes(files.get(f"{info}/METADATA", b""))
    if metadata.get("Name") != "lsquare":
        problems.append(f"the wheel is named {metadata.get('Name')!r}")
    if metadata.get_all("Requires-Dist"):
        problems.append(f"the wheel has runtime dependencies: {metadata.get_all('Requires-Dist')}")
    if metadata.get("Requires-Python") != ">=3.10":
        problems.append(f"Requires-Python is {metadata.get('Requires-Python')!r}")
    return problems


def sdist_problems(files: dict[str, bytes]) -> list[str]:
    return unexpected(files, {*SDIST, *(f"src/{name}" for name in PACKAGE)}) + forbidden(files)


def check_wheel(wheel: Path) -> dict[str, bytes]:
    files = wheel_files(wheel)
    for problem in wheel_problems(files):
        fail(f"{wheel.name}: {problem}")
    print(f"ok   {wheel.name}: exactly the {len(files)} expected files, none matching the {len(FORBIDDEN)} denylist patterns; no dependencies, Python >=3.10")
    # RECORD lists every file's hash, so it differs only when they do: left out of the comparison.
    return {name: data for name, data in files.items() if not name.endswith("/RECORD")}


def check_sdist(sdist: Path, built: dict[str, bytes]) -> None:
    files = sdist_files(sdist)
    for problem in sdist_problems(files):
        fail(f"{sdist.name}: {problem}")
    with tempfile.TemporaryDirectory() as scratch:
        with tarfile.open(sdist) as archive:
            root = archive.getnames()[0].split("/")[0]
            archive.extractall(scratch, filter="data")
        out = Path(scratch) / "out"
        command = ["uv", "build", "--wheel", "--build-constraints", str(BUILD_CONSTRAINTS), "--require-hashes", "--out-dir", str(out), str(Path(scratch) / root)]
        done = subprocess.run(command, capture_output=True, text=True)
        if done.returncode != 0:
            # A hash that doesn't match build-constraints.txt, or a pin it lacks, stops it here.
            print(done.stdout + done.stderr)
            fail(f"uv couldn't build a wheel from {sdist.name}")
        rebuilt = {name: data for name, data in wheel_files(one(out, "*.whl")).items() if not name.endswith("/RECORD")}
    if rebuilt != built:
        different = sorted(set(rebuilt) ^ set(built)) or sorted(name for name in built if built[name] != rebuilt.get(name))
        fail(f"a wheel built from {sdist.name} differs from the one built from the tree: {different}")
    print(f"ok   {sdist.name}: exactly the {len(files)} expected files, none matching the denylist, and builds the same wheel")


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python scripts/check_package.py <dist directory>")
        raise SystemExit(2)
    directory = Path(sys.argv[1])
    built = check_wheel(one(directory, "lsquare-*.whl"))
    check_sdist(one(directory, "lsquare-*.tar.gz"), built)


if __name__ == "__main__":
    main()
