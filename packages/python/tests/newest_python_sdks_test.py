""".github/scripts/newest_python_sdks.py, the weekly job's pins (mcp-sdk-releases.yml), without the
network: each line's group in the real pyproject.toml, pinned to a release with a Requires-Python as
PyPI gives it.

uv locks the package for every Python it supports, so a line's pin has to hold, on each of them,
exactly where the release supports it. Pinned on all of them, fastmcp 4.2.0a4 and later, which need
3.11, failed the weekly job's lock on 3.10, before any of the wrapper's tests ran.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

SCRIPT = Path(__file__).resolve().parents[3] / ".github" / "scripts" / "newest_python_sdks.py"
_spec = importlib.util.spec_from_file_location("newest_python_sdks", SCRIPT)
assert _spec is not None and _spec.loader is not None
weekly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(weekly)

PYPROJECT = weekly.PYPROJECT.read_text(encoding="utf-8")
#: Requires-Python as releases on PyPI have it: mcp 1.30 and 2.3's, and fastmcp 3.4's; fastmcp
#: 4.2.0a4's and later; an upper bound; one left out; none.
REQUIRES = [">=3.10", ">=3.11", ">=3.11,<3.15", "!=3.11.*,>=3.10", ""]
#: The Pythons uv locks for, those the package supports among these.
PYTHONS = [python for python in ("3.10.0", "3.10.22", "3.11.0", "3.12.9", "3.13.0", "3.14.4", "3.15.0") if python in SpecifierSet(re.findall(r'^requires-python = "([^"]+)"$', PYPROJECT, re.MULTILINE)[0])]


def group(text: str, line: str) -> str:
    """The one requirement in pyproject.toml's group `line`."""
    found = re.findall(rf'^{line} = \["([^"]*)"\]$', text, re.MULTILINE)
    assert len(found) == 1, line
    return str(found[0])


@pytest.mark.parametrize("requires", REQUIRES)
@pytest.mark.parametrize("line", sorted(weekly.LINES))
def test_each_lines_pin_holds_exactly_where_its_release_supports_the_python(line: str, requires: str) -> None:
    package, _ = weekly.LINES[line]
    requirement = Requirement(group(weekly.pinned(PYPROJECT, line, "9.9.9", requires), line))
    assert (requirement.name, str(requirement.specifier)) == (package, "==9.9.9")
    assert "3.10.0" in PYTHONS
    for python in PYTHONS:
        environment = {"python_full_version": python, "python_version": ".".join(python.split(".")[:2])}
        holds = requirement.marker is None or requirement.marker.evaluate(environment)
        assert holds == (python in SpecifierSet(requires)), (python, str(requirement))


def test_the_pin_changes_that_groups_line_alone() -> None:
    after = weekly.pinned(PYPROJECT, "fastmcp4", "4.2.5", ">=3.11")
    changed = [(before, now) for before, now in zip(PYPROJECT.splitlines(), after.splitlines()) if before != now]
    assert len(after.splitlines()) == len(PYPROJECT.splitlines())
    assert changed == [(f'fastmcp4 = ["{group(PYPROJECT, "fastmcp4")}"]', "fastmcp4 = [\"fastmcp==4.2.5; python_full_version >= '3.11'\"]")]


def test_check_says_why_when_the_release_isnt_installed(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def version(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", version)
    with pytest.raises(SystemExit):
        weekly.check("fastmcp4", "4.2.5")
    assert "FAIL fastmcp isn't installed" in capsys.readouterr().out
