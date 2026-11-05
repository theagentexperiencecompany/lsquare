"""The weekly job's view of the Python MCP SDKs (Python spec, section 8): for one SDK line, the
newest release on PyPI, pre-releases included, pinned as that line's group in the job's disposable
checkout, and checked to be the one its tests load. Needs `packaging` (`uv run --with packaging`).

    pick  <line>            writes version=<newest> to $GITHUB_OUTPUT, and a line to $GITHUB_STEP_SUMMARY
    apply <line> <version>  pins packages/python's group <line> to exactly that version
    check <line> <version>  run with the line's venv: fails unless the package it tests is that
                            version, loaded from the venv; prints the mcp it runs on

Lines: mcp1 (mcp 1.x), mcp2 (mcp 2.x), fastmcp3 (fastmcp 3.x), fastmcp4 (fastmcp 4.x).
"""

from __future__ import annotations

import json
import os
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


def apply(name: str, version: str) -> None:
    package, _ = line_of(name)
    text = PYPROJECT.read_text(encoding="utf-8")
    pattern = re.compile(rf'^{name} = \["{package}[^"]*"\]$', re.MULTILINE)
    if len(pattern.findall(text)) != 1:
        fail(f"pyproject.toml has no single group {name} for {package}")
    PYPROJECT.write_text(pattern.sub(f'{name} = ["{package}=={version}"]', text), encoding="utf-8")
    print(f"{name} pinned to {package}=={version}")


def check(name: str, version: str) -> None:
    import importlib
    import importlib.metadata

    package, _ = line_of(name)
    installed = importlib.metadata.version(package)
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
