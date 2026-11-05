"""scripts/check_package.py, on archives made here: the wheel and the sdist hold exactly the
expected files, and none of them matches the denylist. CI runs the script on the built ones. And
the hashed pins CI builds and checks them with (build-constraints.txt, twine-requirements.txt)."""

from __future__ import annotations

import io
import re
import subprocess
import sys
import tarfile
import warnings
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_package  # noqa: E402

INFO = "lsquare-0.2.0b3.dist-info"
METADATA = b"Metadata-Version: 2.4\nName: lsquare\nVersion: 0.2.0b3\nRequires-Python: >=3.10\n"


def wheel() -> dict[str, bytes]:
    files = {name: b"" for name in check_package.PACKAGE}
    files.update({f"{INFO}/{name}": b"" for name in check_package.DIST_INFO})
    files[f"{INFO}/METADATA"] = METADATA
    return files


def sdist() -> dict[str, bytes]:
    files = {name: b"" for name in check_package.SDIST}
    files.update({f"src/{name}": b"" for name in check_package.PACKAGE})
    return files


def test_the_expected_files_pass() -> None:
    assert check_package.wheel_problems(wheel()) == []
    assert check_package.sdist_problems(sdist()) == []


def test_what_the_default_sdist_took_is_refused() -> None:
    for extra in ("RELEASING.md", "uv.lock", "scripts/release_check.py", "tests/turns_test.py", "tests/wrapper/lines.py"):
        assert check_package.sdist_problems({**sdist(), extra: b""}) == [f"unexpected files: [{extra!r}]"]


def test_a_file_added_to_the_package_is_refused_in_both_archives() -> None:
    assert check_package.wheel_problems({**wheel(), "lsquare/notes.txt": b""}) == ["unexpected files: ['lsquare/notes.txt']"]
    assert check_package.sdist_problems({**sdist(), "src/lsquare/notes.txt": b""}) == ["unexpected files: ['src/lsquare/notes.txt']"]


def test_a_missing_file_is_refused() -> None:
    files = wheel()
    del files["lsquare/py.typed"]
    assert check_package.wheel_problems(files) == ["missing files: ['lsquare/py.typed']"]
    files = sdist()
    del files["REFERENCE.md"]
    assert check_package.sdist_problems(files) == ["missing files: ['REFERENCE.md']"]


def test_a_forbidden_value_in_an_expected_file_is_refused() -> None:
    for name, text in (
        ("lsquare/_client.py", b'TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"'),
        (f"{INFO}/METADATA", METADATA + b"Author-email: someone@example.com\n"),
        (f"{INFO}/RECORD", b"lsquare/_client.py,sha256=x,1\n/Users/someone/lsquare/_v1.py,,\n"),
        (f"{INFO}/licenses/LICENSE", b"account 0123456789abcdef0123456789abcdef"),
    ):
        problems = check_package.wheel_problems({**wheel(), name: text})
        assert len(problems) == 1 and problems[0].startswith(f"{name} matches "), problems
    for name, text in (("README.md", b"Mail someone@example.com."), (".gitignore", b".venv/\n/home/someone/\n")):
        problems = check_package.sdist_problems({**sdist(), name: text})
        assert len(problems) == 1 and problems[0].startswith(f"{name} matches "), problems


def test_the_redaction_prefix_isnt_a_webhook_but_a_webhook_is_refused() -> None:
    prefix = b'_SECRET_LITERALS = ("hooks.slack.com/services/", "lsq_", "sk-", "-----BEGIN ")'
    assert check_package.wheel_problems({**wheel(), "lsquare/_redact.py": prefix}) == []
    webhook = b"https://hooks.slack.com/services/T0ABCDEFGH/B0ABCDEFGH/abcdefghijklmnop"
    assert len(check_package.wheel_problems({**wheel(), "lsquare/_redact.py": webhook})) == 1


def tar(path: Path, entries: list[tuple[str, bytes]]) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for name, data in entries:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return path


def test_an_sdist_is_read_without_its_root_directory(tmp_path: Path) -> None:
    path = tar(tmp_path / "lsquare-0.2.0b3.tar.gz", [("lsquare-0.2.0b3/README.md", b"ok"), ("lsquare-0.2.0b3/src/lsquare/__init__.py", b"ok")])
    assert check_package.sdist_files(path) == {"README.md": b"ok", "src/lsquare/__init__.py": b"ok"}


def test_a_name_held_twice_or_a_second_root_is_refused(tmp_path: Path) -> None:
    # Read into a dictionary, only the last of two entries would be checked: a forbidden one first.
    twice = [("lsquare-0.2.0b3/README.md", b"ghp_abcdefghijklmnopqrstuvwxyz0123456789"), ("lsquare-0.2.0b3/README.md", b"ok")]
    with pytest.raises(SystemExit):
        check_package.sdist_files(tar(tmp_path / "twice.tar.gz", twice))
    roots = [("lsquare-0.2.0b3/README.md", b"ok"), ("other/README.md", b"ghp_abcdefghijklmnopqrstuvwxyz0123456789")]
    with pytest.raises(SystemExit):
        check_package.sdist_files(tar(tmp_path / "roots.tar.gz", roots))
    path = tmp_path / "lsquare-0.2.0b3-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive, warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns of the duplicate name, and writes it.
        archive.writestr("lsquare/_client.py", "ghp_abcdefghijklmnopqrstuvwxyz0123456789")
        archive.writestr("lsquare/_client.py", "ok")
    with pytest.raises(SystemExit):
        check_package.wheel_files(path)


ROOT = Path(__file__).resolve().parent.parent
#: A pin as `uv pip compile --generate-hashes` writes it: `name==version`, its markers, then a
#: continuation line per hash.
PIN = re.compile(r"([A-Za-z0-9._-]+)==(\S+)(?: ;[^\\]*)? \\")


def pins(name: str) -> dict[str, list[str]]:
    """Each `name==version` a hashed requirements file pins, with its SHA-256s."""
    found: dict[str, list[str]] = {}
    current = None
    for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("    --hash=sha256:"):
            assert current is not None, line
            found[current].append(line.removeprefix("    --hash=sha256:").removesuffix(" \\"))
            continue
        match = PIN.fullmatch(line)
        assert match is not None, f"{name}: {line!r} isn't an exact pin"
        current = f"{match.group(1).lower()}=={match.group(2)}"
        found.setdefault(current, [])
    assert found, name
    for pin, digests in found.items():
        assert digests and all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in digests), f"{name}: {pin} has no SHA-256s, or one that isn't"
    return found


def test_the_build_constraints_pin_pyprojects_hatchling_and_what_it_imports_by_hash() -> None:
    requires = re.search(r'^requires = \["hatchling==([^"]+)"\]$', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.MULTILINE)
    assert requires is not None
    constraints = pins("build-constraints.txt")
    # A hatchling moved in pyproject.toml without regenerating the file fails here.
    assert f"hatchling=={requires.group(1)}" in constraints
    assert {pin.split("==")[0] for pin in constraints} >= {"hatchling", "packaging", "pathspec", "pluggy", "trove-classifiers"}


def test_twine_and_what_it_imports_are_pinned_by_hash() -> None:
    assert "twine==7.0.0" in pins("twine-requirements.txt")


def test_the_wheel_from_the_sdist_is_built_with_the_repositorys_pins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tar(tmp_path / "lsquare-0.2.0b3.tar.gz", [(f"lsquare-0.2.0b3/{name}", data) for name, data in sdist().items()])
    commands: list[list[str]] = []

    def run(command: list[str], **_: object) -> None:
        commands.append(command)
        raise SystemExit(0)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SystemExit):
        check_package.check_sdist(path, {})
    [command] = commands
    assert command[:3] == ["uv", "build", "--wheel"]
    assert command[command.index("--build-constraints") + 1] == str(ROOT / "build-constraints.txt")
    assert "--require-hashes" in command
