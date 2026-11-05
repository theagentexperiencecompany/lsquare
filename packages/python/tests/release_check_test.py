"""scripts/release_check.py, the release's checks (RELEASING.md), without the network: the index's
and GitHub's answers are stubbed where the script asks for them, and call() itself, which asks, is
tested against a server on 127.0.0.1."""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import sys
import tarfile
import threading
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import release_check  # noqa: E402

#: The real one: every other test stubs it.
SOURCE_VERSION = release_check.source_version
VERSION = "0.2.0b1"
SHA = "a" * 40
WHEEL = f"lsquare-{VERSION}-py3-none-any.whl"
SDIST = f"lsquare-{VERSION}.tar.gz"


@pytest.fixture(autouse=True)
def run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key, value in {
        "VERSION": VERSION,
        "INDEX": "pypi",
        "RECOVER": "false",
        "COMMIT": "",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": SHA,
        "GITHUB_REPOSITORY": "owner/repo",
        "GH_TOKEN": "token",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TESTED", raising=False)
    monkeypatch.setattr(release_check, "source_version", lambda: VERSION)
    monkeypatch.setattr(release_check, "POLL_S", 0)


def answering(monkeypatch: pytest.MonkeyPatch, answers: dict[str, Any]) -> list[tuple[str, str]]:
    """Stubs `call`: each URL's answer, a (status, body) or an exception to raise."""
    asked: list[tuple[str, str]] = []

    def call(method: str, url: str, token: str | None = None, body: Any = None, answers_: frozenset[int] = frozenset({404})) -> tuple[int, Any]:
        asked.append((method, url))
        answer = answers[url]
        if isinstance(answer, Exception):
            raise answer
        return cast(tuple[int, Any], answer)

    monkeypatch.setattr(release_check, "call", call)
    return asked


def index_url(index: str = "pypi") -> str:
    return f"{release_check.INDEXES[index][0]}/lsquare/{VERSION}/json"


TAG_URL = "https://api.github.com/repos/owner/repo/git/ref/tags/python-v0.2.0b1"


def files(*names: tuple[str, str]) -> tuple[int, Any]:
    return 200, {"urls": [{"filename": name, "digests": {"sha256": digest}} for name, digest in names]}


def dist(tmp_path: Path, version: str = VERSION, sdist_version: str | None = None) -> Path:
    """A wheel and an sdist whose metadata say `version` (the sdist's, `sdist_version`)."""
    out = tmp_path / "dist"
    out.mkdir(parents=True)
    with zipfile.ZipFile(out / f"lsquare-{version}-py3-none-any.whl", "w") as wheel:
        wheel.writestr(f"lsquare-{version}.dist-info/METADATA", f"Metadata-Version: 2.4\nName: lsquare\nVersion: {version}\n")
    info = f"Metadata-Version: 2.4\nName: lsquare\nVersion: {sdist_version or version}\n".encode()
    with tarfile.open(out / f"lsquare-{version}.tar.gz", "w:gz") as sdist:
        member = tarfile.TarInfo(f"lsquare-{version}/PKG-INFO")
        member.size = len(info)
        sdist.addfile(member, io.BytesIO(info))
    return out


def made(monkeypatch: pytest.MonkeyPatch, folder: Path) -> None:
    """TESTED says python-build made the files in `folder`, as sha256sum writes their SHA-256s."""
    lines = [f"{release_check.sha256(path)}  {path.name}" for path in sorted(folder.iterdir()) if not path.name.startswith(".")]
    monkeypatch.setenv("TESTED", "\n".join(lines))


def fails(step: Any, *args: Any) -> None:
    with pytest.raises(SystemExit):
        step(*args)


def serving(raw: list[bytes]) -> str:
    """A server on 127.0.0.1 that answers each connection with the next of `raw`, as bytes on the
    wire, then closes it, and stops after the last: its base URL."""
    server = socket.create_server(("127.0.0.1", 0))

    def serve() -> None:
        with server:
            for answer in raw:
                connection, _ = server.accept()
                with connection, contextlib.suppress(OSError):
                    connection.recv(65536)
                    connection.sendall(answer)

    threading.Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{server.getsockname()[1]}"


def answer(body: bytes, length: int | None = None, status: bytes = b"HTTP/1.1 200 OK") -> bytes:
    """An HTTP answer whose Content-Length says `length`, by default the body's."""
    head = b"%s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % (status, len(body) if length is None else length)
    return head + body


class TestCall:
    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param(answer(b'{"urls": [', length=200), id="a body cut short"),
            pytest.param(b"HTTP/1.1 ???\r\n\r\n", id="a status line that isn't one"),
            pytest.param(b"HTTP/1.1 200 OK\r\nX-Long: " + b"a" * 70_000 + b"\r\n\r\n", id="a header line too long"),
            pytest.param(answer(b"<html>busy</html>"), id="a body that isn't JSON"),
            pytest.param(answer(b"", status=b"HTTP/1.1 503 Service Unavailable"), id="a 5xx"),
            pytest.param(b"", id="no answer at all"),
        ],
    )
    def test_anything_but_an_answer_is_unreachable(self, raw: bytes) -> None:
        with pytest.raises(release_check.Unreachable):
            release_check.call("GET", serving([raw]) + "/pypi/lsquare/0.2.0b1/json")

    def test_a_json_body_and_a_listed_status_are_answers(self) -> None:
        url = serving([answer(b'{"urls": []}'), answer(b"", status=b"HTTP/1.1 404 Not Found")]) + "/x"
        assert release_check.call("GET", url) == (200, {"urls": []})
        assert release_check.call("GET", url) == (404, None)

    def test_published_keeps_polling_past_a_body_cut_short(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        folder = dist(tmp_path)
        made(monkeypatch, folder)
        body = json.dumps(files((WHEEL, release_check.sha256(folder / WHEEL)), (SDIST, release_check.sha256(folder / SDIST)))[1]).encode()
        base = serving([answer(body[:20], length=len(body)), answer(body)])
        monkeypatch.setitem(release_check.INDEXES, "pypi", (base + "/pypi", base + "/simple/"))
        # Seconds, not ten minutes, if it never gets the second answer.
        monkeypatch.setattr(release_check, "PROPAGATION_S", 5)
        release_check.published(folder)
        assert "byte for byte the files CI tested" in (tmp_path / "summary.md").read_text()


class TestInputs:
    @pytest.mark.parametrize("version", ["0.2.0b1", "0.2.0b12"])
    def test_takes_a_beta_of_0_2_0(self, version: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERSION", version)
        assert release_check.inputs() == (version, "pypi", False)

    @pytest.mark.parametrize("version", ["0.2.0", "0.2.0b0", "0.2.0b01", "0.2.0rc1", "0.2.0b1.dev0", "0.2.0.b1", "0.3.0b1", " 0.2.0b1", ""])
    def test_refuses_anything_else_a_stable_release_included(self, version: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERSION", version)
        fails(release_check.inputs)

    def test_refuses_an_unknown_index_and_reads_recover(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RECOVER", "true")
        assert release_check.inputs()[2] is True
        monkeypatch.setenv("INDEX", "pypi.org")
        fails(release_check.inputs)


class TestPreflight:
    def test_passes_for_a_new_version_with_no_tag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        asked = answering(monkeypatch, {index_url(): (404, None), TAG_URL: (404, None)})
        release_check.preflight()
        assert asked == [("GET", index_url()), ("GET", TAG_URL)]

    def test_on_testpypi_reads_no_tag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INDEX", "testpypi")
        asked = answering(monkeypatch, {index_url("testpypi"): (404, None)})
        release_check.preflight()
        assert asked == [("GET", index_url("testpypi"))]

    def test_refuses_another_branch_and_a_version_that_isnt_the_sources(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answering(monkeypatch, {index_url(): (404, None), TAG_URL: (404, None)})
        monkeypatch.setenv("GITHUB_REF", "refs/heads/main.2")
        fails(release_check.preflight)
        monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
        monkeypatch.setattr(release_check, "source_version", lambda: "0.2.0b2.dev0")
        fails(release_check.preflight)

    def test_refuses_a_version_the_index_has_unless_recovering_and_recovering_one_it_hasnt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answering(monkeypatch, {index_url(): files((WHEEL, "1" * 64)), TAG_URL: (404, None)})
        fails(release_check.preflight)
        monkeypatch.setenv("RECOVER", "true")
        monkeypatch.setenv("COMMIT", SHA)
        release_check.preflight()
        answering(monkeypatch, {index_url(): (404, None), TAG_URL: (404, None)})
        fails(release_check.preflight)

    def test_stops_when_the_index_doesnt_answer_rather_than_taking_it_for_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answering(monkeypatch, {index_url(): release_check.Unreachable("GET answered 503"), TAG_URL: (404, None)})
        fails(release_check.preflight)

    def test_recovers_only_on_the_releases_own_commit_and_takes_a_commit_only_to_recover(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answering(monkeypatch, {index_url(): files((WHEEL, "1" * 64)), TAG_URL: (404, None)})
        monkeypatch.setenv("RECOVER", "true")
        fails(release_check.preflight)
        monkeypatch.setenv("COMMIT", "b" * 40)
        fails(release_check.preflight)
        monkeypatch.setenv("COMMIT", SHA)
        release_check.preflight()
        answering(monkeypatch, {index_url(): (404, None), TAG_URL: (404, None)})
        monkeypatch.setenv("RECOVER", "false")
        fails(release_check.preflight)

    def test_refuses_a_tag_on_another_commit_and_takes_one_on_this(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answering(monkeypatch, {index_url(): (404, None), TAG_URL: (200, {"object": {"type": "commit", "sha": "b" * 40}})})
        fails(release_check.preflight)
        answering(monkeypatch, {index_url(): (404, None), TAG_URL: (200, {"object": {"type": "tag", "sha": SHA}})})
        fails(release_check.preflight)
        answering(monkeypatch, {index_url(): (404, None), TAG_URL: (200, {"object": {"type": "commit", "sha": SHA}})})
        release_check.preflight()


class TestArtifacts:
    def test_passes_for_this_versions_two_files_and_writes_their_hashes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        answering(monkeypatch, {index_url(): (404, None)})
        folder = dist(tmp_path)
        made(monkeypatch, folder)
        release_check.artifacts(folder)
        summary = (tmp_path / "summary.md").read_text()
        assert release_check.sha256(folder / WHEEL) in summary and release_check.sha256(folder / SDIST) in summary

    def test_refuses_another_file_another_version_or_metadata_that_disagrees(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        answering(monkeypatch, {index_url(): (404, None)})
        folder = dist(tmp_path / "extra")
        (folder / "notes.txt").write_text("x")
        made(monkeypatch, folder)
        fails(release_check.artifacts, folder)
        for folder in (dist(tmp_path / "other", version="0.2.0b2"), dist(tmp_path / "disagreeing", sdist_version="0.2.0b2")):
            made(monkeypatch, folder)
            fails(release_check.artifacts, folder)

    def test_refuses_files_that_arent_the_ones_python_build_made(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        asked = answering(monkeypatch, {index_url(): (404, None)})
        folder = dist(tmp_path)
        made(monkeypatch, folder)
        recorded = os.environ["TESTED"]
        # Another wheel, with the same name and metadata: what an artifact replaced in the run holds.
        with zipfile.ZipFile(folder / WHEEL, "a") as wheel:
            wheel.writestr("lsquare/_planted.py", "")
        fails(release_check.artifacts, folder)
        made(monkeypatch, folder)
        release_check.artifacts(folder)
        first = recorded.splitlines()[0]
        for wrong in ("", first, recorded + "\n" + first, recorded.replace("  ", " "), "0" * 64 + "  ../" + WHEEL):
            monkeypatch.setenv("TESTED", wrong)
            fails(release_check.artifacts, folder)
        monkeypatch.delenv("TESTED")
        fails(release_check.artifacts, folder)
        assert asked == [("GET", index_url())]

    def test_recovering_uploads_only_whats_missing_and_only_onto_this_builds_files(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("RECOVER", "true")
        folder = dist(tmp_path)
        made(monkeypatch, folder)
        wheel = release_check.sha256(folder / WHEEL)
        answering(monkeypatch, {index_url(): files((WHEEL, wheel))})
        release_check.artifacts(folder)
        assert f"Missing from pypi: {SDIST}." in (tmp_path / "summary.md").read_text()
        answering(monkeypatch, {index_url(): files((WHEEL, "0" * 64))})
        fails(release_check.artifacts, folder)
        answering(monkeypatch, {index_url(): files((WHEEL, wheel), (SDIST, release_check.sha256(folder / SDIST)))})
        fails(release_check.artifacts, folder)


class TestPublished:
    def test_waits_for_the_index_then_passes_on_the_tested_bytes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        folder = dist(tmp_path)
        made(monkeypatch, folder)
        both = files((WHEEL, release_check.sha256(folder / WHEEL)), (SDIST, release_check.sha256(folder / SDIST)))
        answers: Iterator[Any] = iter([(404, None), release_check.Unreachable("timed out"), files((WHEEL, release_check.sha256(folder / WHEEL))), both])

        def call(method: str, url: str, token: str | None = None, body: Any = None, answers_: frozenset[int] = frozenset({404})) -> tuple[int, Any]:
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return cast(tuple[int, Any], answer)

        monkeypatch.setattr(release_check, "call", call)
        release_check.published(folder)
        assert next(answers, None) is None

    def test_fails_at_once_on_a_file_that_isnt_the_tested_one(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        answering(monkeypatch, {index_url(): files((WHEEL, "0" * 64))})
        folder = dist(tmp_path)
        made(monkeypatch, folder)
        fails(release_check.published, folder)

    def test_gives_up_after_the_propagation_time(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        answering(monkeypatch, {index_url(): (404, None)})
        monkeypatch.setattr(release_check, "PROPAGATION_S", 0)
        folder = dist(tmp_path)
        made(monkeypatch, folder)
        fails(release_check.published, folder)

    def test_compares_the_index_only_with_what_python_build_made(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        asked = answering(monkeypatch, {index_url(): (404, None)})
        monkeypatch.setattr(release_check, "PROPAGATION_S", 0)
        monkeypatch.setenv("TESTED", f"{'0' * 64}  {WHEEL}\n{'0' * 64}  {SDIST}")
        fails(release_check.published, dist(tmp_path))
        assert asked == []


class TestInstalls:
    def installing(self, monkeypatch: pytest.MonkeyPatch, *, runtime: str = VERSION, inside: bool = True, plain: str = VERSION) -> list[tuple[str, str]]:
        """Stubs the venvs and pip: the pinned install says `runtime`, from inside the temporary
        directory or not, and a plain install gets `plain`."""
        installed: list[tuple[str, str]] = []

        def venv(root: Path, name: str) -> Path:
            return root / name / "bin" / "python"

        def pip_install(python: Path, requirement: str, simple: str, until: float) -> None:
            installed.append((python.parent.parent.name, requirement + " " + simple))

        def isolated(python: Path, code: str, cwd: Path) -> str:
            if python.parent.parent.name == "plain":
                return plain
            module = (cwd if inside else Path("/elsewhere")) / "pinned" / "lsquare" / "__init__.py"
            return json.dumps({"runtime": runtime, "distribution": VERSION, "file": str(module)})

        for name, stub in (("venv", venv), ("pip_install", pip_install), ("isolated", isolated)):
            monkeypatch.setattr(release_check, name, stub)
        return installed

    def test_installs_the_pinned_version_and_a_plain_install_gets_it_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        installed = self.installing(monkeypatch)
        release_check.installs()
        assert installed == [("pinned", f"lsquare=={VERSION} https://pypi.org/simple/"), ("plain", "lsquare https://pypi.org/simple/")]

    def test_on_testpypi_installs_from_it_and_skips_the_plain_install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INDEX", "testpypi")
        installed = self.installing(monkeypatch)
        release_check.installs()
        assert installed == [("pinned", f"lsquare=={VERSION} https://test.pypi.org/simple/")]

    @pytest.mark.parametrize("plain", ["0.0.1", "0.2.0b0", "0.2.0b2"])
    def test_fails_when_a_plain_install_gets_another_version(self, monkeypatch: pytest.MonkeyPatch, plain: str) -> None:
        self.installing(monkeypatch, plain=plain)
        fails(release_check.installs)

    def test_fails_on_another_version_or_a_module_outside_the_fresh_venv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.installing(monkeypatch, runtime="0.2.0.dev0")
        fails(release_check.installs)
        self.installing(monkeypatch, inside=False)
        fails(release_check.installs)


class TestTag:
    REFS = "https://api.github.com/repos/owner/repo/git/refs"

    def test_tags_this_runs_commit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        asked = answering(monkeypatch, {self.REFS: (201, {})})
        release_check.tag()
        assert asked == [("POST", self.REFS)]

    def test_takes_the_tag_already_on_this_commit_and_never_moves_one_elsewhere(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answering(monkeypatch, {self.REFS: (422, None), TAG_URL: (200, {"object": {"type": "commit", "sha": SHA}})})
        release_check.tag()
        answering(monkeypatch, {self.REFS: (422, None), TAG_URL: (200, {"object": {"type": "commit", "sha": "b" * 40}})})
        fails(release_check.tag)

    def test_tags_only_a_release_to_pypi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INDEX", "testpypi")
        answering(monkeypatch, {})
        fails(release_check.tag)


def test_reads_the_version_the_package_says() -> None:
    from lsquare import __version__

    assert SOURCE_VERSION() == __version__
