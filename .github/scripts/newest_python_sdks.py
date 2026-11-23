"""The weekly job's view of the Python MCP SDKs (Python spec, section 8): for one SDK line, the
newest release on PyPI, pre-releases included, pinned as that line's group in the job's disposable
checkout, and checked to be the one its tests load. Needs `packaging` (`uv run --with packaging`).

    pick  <line>            writes version=<newest> to $GITHUB_OUTPUT, and a line to $GITHUB_STEP_SUMMARY
    apply <line> <version>  pins packages/python's group <line> to exactly that version, on the
                            Pythons its Requires-Python on PyPI includes
    check <line> <version>  run with the line's venv: fails unless the package it tests is that
                            version, loaded from the venv; prints the mcp it runs on

Lines: mcp1 (mcp 1.x), mcp2 (mcp 2.x), fastmcp3 (fastmcp 3.x), fastmcp4 (fastmcp 4.x).
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PYPROJECT = ROOT / "packages" / "python" / "pyproject.toml"
#: Each line: the package it tests and its major version.
LINES = {"mcp1": ("mcp", 1), "mcp2": ("mcp", 2), "fastmcp3": ("fastmcp", 3), "fastmcp4": ("fastmcp", 4)}


def fail(message: str) -> None:
    print(f"FAIL {message}")
    raise SystemExit(1)


def line_of(name: str) -> tuple[str, int]:
    if name not in LINES:
        fail(f"no line {name!r}: one of {', '.join(LINES)}")
    return LINES[name]


def newest(package: str, major: int) -> str:
    """The newest release of `package` within `major`, by PEP 440, pre-releases included; a
    release whose every file is yanked doesn't count."""
    from packaging.version import InvalidVersion, Version

    request = urllib.request.Request(f"https://pypi.org/pypi/{package}/json", headers={"User-Agent": "lsquare-weekly-check"})
    with urllib.request.urlopen(request, timeout=30) as response:
        releases = json.load(response)["releases"]
    candidates = []
    for text, files in releases.items():
        try:
            version = Version(text)
        except InvalidVersion:
            continue
        if version.major != major or not files or all(file.get("yanked") for file in files):
            continue
        candidates.append(version)
    if not candidates:
        fail(f"PyPI has no {package} {major}.x release")
    return str(max(candidates))


def output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as file:
            file.write(f"{key}={value}\n")


def pick(name: str) -> None:
    package, major = line_of(name)
    version = newest(package, major)
    output("version", version)
    print(f"{name}: {package} {version}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as file:
            file.write(f"- {name}: `{package}=={version}`\n")


def requires_python(package: str, version: str) -> str:
    """The Pythons that release of `package` supports, as its Requires-Python on PyPI says: "" when
    it says none."""
    request = urllib.request.Request(f"https://pypi.org/pypi/{package}/{version}/json", headers={"User-Agent": "lsquare-weekly-check"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["info"].get("requires_python") or ""


def requirement(package: str, version: str, python: str) -> str:
    """The pin of exactly that release, on the Pythons its Requires-Python, `python`, includes. uv
    locks the package for every Python the package supports, from 3.10: a release that needs a newer
    one (fastmcp 4.2.0a4 and later need 3.11) fails the whole lock when pinned on all of them, and
    nothing is tested. On the Pythons it leaves out, the group pins nothing."""
    from packaging.specifiers import SpecifierSet

    markers = [f"python_full_version {spec.operator} '{spec.version}'" for spec in sorted(SpecifierSet(python), key=str)]
    return f"{package}=={version}" + (f"; {' and '.join(markers)}" if markers else "")


def pinned(text: str, name: str, version: str, python: str) -> str:
    """pyproject.toml's `text` with group `name` pinned to that release (`requirement`)."""
    package, _ = line_of(name)
    pattern = re.compile(rf'^{name} = \["{package}[^"]*"\]$', re.MULTILINE)
    if len(pattern.findall(text)) != 1:
        fail(f"pyproject.toml has no single group {name} for {package}")
    return pattern.sub(lambda _: f'{name} = ["{requirement(package, version, python)}"]', text)


def apply(name: str, version: str) -> None:
    package, _ = line_of(name)
    python = requires_python(package, version)
    PYPROJECT.write_text(pinned(PYPROJECT.read_text(encoding="utf-8"), name, version, python), encoding="utf-8")
    print(f"{name} pinned to {requirement(package, version, python)}")


def check(name: str, version: str) -> None:
    import importlib
    import importlib.metadata

    package, _ = line_of(name)
    try:
        installed = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        fail(f"{package} isn't installed, on the venv's Python {platform.python_version()}: its pin holds only where {version}'s Requires-Python does")
    if installed != version:
        fail(f"{package} {installed} is installed, not the {version} picked")
    module = importlib.import_module(package)
    location = Path(module.__file__ or "").resolve()
    if Path(sys.prefix).resolve() not in location.parents:
        fail(f"{package} loads from {location}, outside the venv at {sys.prefix}")
    mcp = importlib.metadata.version("mcp")
    print(f"{name}: {package} {installed}" + (f" on mcp {mcp}" if package != "mcp" else ""))
    output("mcp", mcp)


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["pick"] and len(args) == 2:
        pick(args[1])
    elif args[:1] == ["apply"] and len(args) == 3:
        apply(args[1], args[2])
    elif args[:1] == ["check"] and len(args) == 3:
        check(args[1], args[2])
    else:
        print(__doc__)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
