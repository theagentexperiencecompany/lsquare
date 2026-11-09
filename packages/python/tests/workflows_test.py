"""The repository's workflows (.github/workflows), read with PyYAML: what they let reach GitHub's
cache and the token, which files a release publishes, and which pull requests skip the tests.

- Nothing in a release (pypi-release.yml, the ci.yml it calls included) or in the weekly job
  (mcp-sdk-releases.yml), which runs releases nobody has vetted, gets any cache access:
  `cache-mode: none`, which GitHub's cache service enforces whatever a step runs, and the setup
  actions told so too. ci.yml declares no mode of its own: pull requests and pushes keep GitHub's
  defaults. An action not listed in ACTIONS fails here until someone has checked what it caches.
- No checkout keeps the token.
- Every job that takes the Python package takes python-build's artifact by its ID, checked first,
  and its files by the SHA-256s python-build recorded once its pinned build and checks were done.
- Only the release's `publish` job can get a token for PyPI, and it waits for the `pypi`
  environment, whose required reviewer approves each release.
- The `changes` step, run on scratch repositories: only a pull request into main that changes
  Markdown alone, and not packages/contract/REDACTION.md, skips the test suites.
- Every job that runs the workspaces' code builds packages/contract first, and a pull request that
  changes it must raise its version, by which LSquare's ingestion takes it: checked in a job
  of its own, by a script run on scratch repositories too.
- The contract's smoke check runs on each of the core job's Node versions its engines include.
- all checks passed waits for every other job in ci.yml, and fails if one failed or was cancelled,
  so a branch rule can require it alone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
CONTRACT_VERSION = WORKFLOWS.parent / "scripts" / "contract-version.mjs"
NAMES = sorted(path.name for path in WORKFLOWS.glob("*.yml"))
#: Every action the workflows use, and what it does with GitHub's cache.
ACTIONS = {
    "actions/checkout": "nothing",
    "actions/setup-node": "npm's, when `cache` names npm or, unless package-manager-cache is false, by itself",
    "astral-sh/setup-uv": "uv's, unless enable-cache is false",
    "actions/download-artifact": "nothing",
    "actions/upload-artifact": "nothing",
    "pypa/gh-action-pypi-publish": "nothing",
}
#: ci.yml caches on pull requests and pushes, never when pypi-release.yml calls it.
CI_UV = "${{ inputs.release && 'false' || 'auto' }}"
CI_NPM = "${{ !inputs.release && 'npm' || '' }}"
#: The npm packages' artifacts, taken by name: lsquare's, and @lsquare/node 0.1's and the contract's
#: for their smoke checks.
NPM_ARTIFACTS = {"lsquare-package", "lsquare-node-package", "lsquare-contract-package"}
#: Where each workflow finds python-build's artifact ID and SHA-256s.
IDENTITY = {
    "ci.yml": ("python-build", "${{ needs.python-build.outputs.artifact-id }}", "${{ needs.python-build.outputs.sha256 }}"),
    "pypi-release.yml": ("ci", "${{ needs.ci.outputs.python-artifact-id }}", "${{ needs.ci.outputs.python-sha256 }}"),
}


def workflow(name: str) -> dict[str, Any]:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def jobs(name: str) -> dict[str, dict[str, Any]]:
    return dict(workflow(name)["jobs"])


def triggers(name: str) -> dict[str, Any]:
    """Its `on:`, which PyYAML, reading YAML 1.1, takes for the key True."""
    data: dict[Any, Any] = workflow(name)
    return dict(data[True])


def action(step: dict[str, Any]) -> str:
    """The action a step uses, without its version: empty for a `run:` step."""
    return str(step.get("uses", "")).split("@", 1)[0]


def using(name: str, wanted: str) -> list[tuple[str, dict[str, Any]]]:
    """Each step of `name` that uses `wanted`, with its job and `with:` inputs."""
    return [(job_id, dict(step.get("with") or {})) for job_id, job in jobs(name).items() for step in job.get("steps", []) if action(step) == wanted]


def needs(job: dict[str, Any]) -> list[str]:
    value = job.get("needs", [])
    return [value] if isinstance(value, str) else list(value)


@pytest.mark.parametrize("name", NAMES)
def test_every_action_is_one_whose_caching_is_known(name: str) -> None:
    used = {action(step) for job in jobs(name).values() for step in job.get("steps", []) if "uses" in step}
    assert used - set(ACTIONS) == set(), "check what these do with GitHub's cache, then list them in ACTIONS"


def test_a_release_and_the_weekly_job_get_no_cache_access() -> None:
    for name in ("pypi-release.yml", "mcp-sdk-releases.yml"):
        assert workflow(name).get("cache-mode") == "none", name
        assert {job.get("cache-mode", "none") for job in jobs(name).values()} == {"none"}, name
    # A caller's own mode limits the workflow it calls: set on the calling job too.
    callers = [(name, job) for name in NAMES for job in jobs(name).values() if job.get("uses") == "./.github/workflows/ci.yml"]
    assert [name for name, _ in callers] == ["pypi-release.yml"]
    assert all(job.get("cache-mode") == "none" for _, job in callers)
    # A mode ci.yml asked for beyond its caller's would stop the release before it starts; and
    # pull requests and pushes keep GitHub's defaults.
    assert "cache-mode" not in workflow("ci.yml")
    assert [job_id for job_id, job in jobs("ci.yml").items() if "cache-mode" in job] == []


def test_their_setup_actions_are_told_to_restore_nothing() -> None:
    for name in ("pypi-release.yml", "mcp-sdk-releases.yml"):
        uv = using(name, "astral-sh/setup-uv")
        assert uv and [inputs.get("enable-cache") for _, inputs in uv] == [False] * len(uv), name
        for _, inputs in using(name, "actions/setup-node"):
            assert "cache" not in inputs and inputs.get("package-manager-cache") is False, name
    uv = using("ci.yml", "astral-sh/setup-uv")
    assert uv
    for job_id, inputs in uv:
        # python-build never takes a cache: uv takes a restored one's word for what it holds.
        assert inputs.get("enable-cache") == (False if job_id == "python-build" else CI_UV), job_id
    node = using("ci.yml", "actions/setup-node")
    assert node
    for job_id, inputs in node:
        assert inputs.get("cache", CI_NPM) == CI_NPM and inputs.get("package-manager-cache") is False, job_id


@pytest.mark.parametrize("name", NAMES)
def test_no_checkout_keeps_the_token(name: str) -> None:
    checkouts = using(name, "actions/checkout")
    assert checkouts
    assert [inputs.get("persist-credentials") for _, inputs in checkouts] == [False] * len(checkouts)


@pytest.mark.parametrize("name", sorted(IDENTITY))
def test_every_job_that_takes_the_python_package_takes_python_builds_files(name: str) -> None:
    source, artifact_id, sha256 = IDENTITY[name]
    taking = set()
    for job_id, job in jobs(name).items():
        steps = job.get("steps", [])
        for at, step in enumerate(steps):
            inputs = step.get("with") or {}
            if action(step) != "actions/download-artifact" or inputs.get("name") in NPM_ARTIFACTS:
                continue
            taking.add(job_id)
            assert inputs == {"artifact-ids": artifact_id, "path": inputs.get("path")}, job_id
            assert source in needs(job), job_id
            # An empty ID would download every artifact of the run.
            assert any(artifact_id in (before.get("env") or {}).values() and "test -n" in before.get("run", "") for before in steps[:at]), job_id
            assert any(sha256 in (after.get("env") or {}).values() for after in steps[at + 1 :]), job_id
    assert taking >= ({"python-core", "python-wrapper", "python-clean-install"} if name == "ci.yml" else {"verify", "publish", "check"})


def test_publish_uploads_only_the_files_python_build_recorded() -> None:
    steps = jobs("pypi-release.yml")["publish"]["steps"]
    upload = [action(step) for step in steps].index("pypa/gh-action-pypi-publish")
    [download] = [step for step in steps if action(step) == "actions/download-artifact"]
    [check] = [step for step in steps[:upload] if "sha256sum --check --strict" in step.get("run", "")]
    assert check["working-directory"] == download["with"]["path"] == steps[upload]["with"]["packages-dir"].rstrip("/")
    # And nothing but those files: the upload takes the whole directory.
    assert "ls -A" in check["run"]


def test_only_publish_can_publish_and_it_waits_for_the_pypi_environment() -> None:
    release = jobs("pypi-release.yml")
    assert release["publish"]["environment"] == "pypi"
    assert [job_id for job_id, job in release.items() if (job.get("permissions") or {}).get("id-token") == "write"] == ["publish"]


def test_python_build_records_the_files_of_its_pinned_build_once_checked() -> None:
    outputs = triggers("ci.yml")["workflow_call"]["outputs"]
    assert outputs["python-artifact-id"]["value"] == "${{ jobs.python-build.outputs.artifact-id }}"
    assert outputs["python-sha256"]["value"] == "${{ jobs.python-build.outputs.sha256 }}"
    job = jobs("ci.yml")["python-build"]
    assert job["outputs"] == {"artifact-id": "${{ steps.upload.outputs.artifact-id }}", "sha256": "${{ steps.files.outputs.sha256 }}"}
    steps = job["steps"]
    runs = [str(step.get("run", "")) for step in steps]
    build = next(at for at, run in enumerate(runs) if run.startswith("uv build"))
    assert "--build-constraints build-constraints.txt --require-hashes" in runs[build]
    check = next(at for at, run in enumerate(runs) if "scripts/check_package.py" in run)
    twine = next(at for at, run in enumerate(runs) if "twine-requirements.txt" in run)
    assert "--require-hashes --only-binary :all: -r twine-requirements.txt" in runs[twine]
    assert 'twine" check --strict dist/*' in runs[twine]
    files = next(at for at, step in enumerate(steps) if step.get("id") == "files")
    assert "sha256sum" in runs[files]
    # Nothing runs beside the files once their SHA-256s are recorded, but the upload.
    assert build < check < twine < files == len(steps) - 2
    assert steps[-1].get("id") == "upload" and action(steps[-1]) == "actions/upload-artifact"


def changes_step() -> dict[str, Any]:
    [step] = [step for step in jobs("ci.yml")["changes"]["steps"] if step.get("id") == "files"]
    return dict(step)


def test_the_changes_step_reads_the_event_through_the_environment() -> None:
    assert changes_step()["env"] == {
        "EVENT": "${{ github.event_name }}",
        "BASE": "${{ github.event.pull_request.base.sha }}",
        "BASE_REF": "${{ github.base_ref }}",
        "RELEASE": "${{ inputs.release }}",
    }
    assert "${{" not in changes_step()["run"]


def isolated(home: Path) -> dict[str, str]:
    """An environment for git that reads none of this machine's settings."""
    return {"PATH": os.environ["PATH"], "HOME": str(home), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(repo: Path, *args: str) -> str:
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]
    return subprocess.run(["git", *identity, *args], cwd=repo, env=isolated(repo), check=True, capture_output=True, text=True).stdout.strip()


#: The base each pull request below changes, with shell commands.
TREE = {"README.md": "x\n", "a.ts": "x\n", "packages/contract/REDACTION.md": "x\n", "docs/packages/contract/REDACTION.md": "x\n"}


@pytest.mark.parametrize(
    ("event", "base_ref", "change", "code"),
    [
        pytest.param("pull_request", "main", "echo more >> README.md", "false", id="a README alone, into main"),
        pytest.param("pull_request", "main", "echo more >> README.md && echo more >> a.ts", "true", id="code too, into main"),
        pytest.param("pull_request", "main", "echo more >> packages/contract/REDACTION.md", "true", id="the redaction spec, which the tests read"),
        pytest.param("pull_request", "main", "git rm -q packages/contract/REDACTION.md", "true", id="the redaction spec deleted"),
        pytest.param("pull_request", "main", "git mv packages/contract/REDACTION.md docs/REDACTION.md", "true", id="the redaction spec moved"),
        pytest.param("pull_request", "main", "echo more >> docs/packages/contract/REDACTION.md", "false", id="another file of that name"),
        pytest.param("pull_request", "feature", "echo more >> README.md", "true", id="a README alone, into another branch"),
        pytest.param("pull_request", "", "echo more >> README.md", "true", id="a README alone, no base branch"),
        pytest.param("push", "", "echo more >> README.md", "true", id="a push"),
        pytest.param("release", "", "echo more >> README.md", "true", id="a release"),
        pytest.param("unknown base", "main", "echo more >> README.md", "true", id="a base the checkout doesn't have"),
    ],
)
def test_which_pull_requests_skip_the_test_suites(tmp_path: Path, event: str, base_ref: str, change: str, code: str) -> None:
    repo = tmp_path / "repo"
    for name, text in TREE.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    subprocess.run(["bash", "-e", "-c", change], cwd=repo, env=isolated(repo), check=True)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "head")
    output = tmp_path / "output"
    environment = {
        **isolated(repo),
        "GITHUB_OUTPUT": str(output),
        "EVENT": "workflow_dispatch" if event == "release" else "pull_request" if event == "unknown base" else event,
        "BASE": "1" * 40 if event == "unknown base" else base if event == "pull_request" else "",
        "BASE_REF": base_ref,
        "RELEASE": "true" if event == "release" else "",
    }
    # As GitHub runs a `run:` step without a `shell:`.
    subprocess.run(["bash", "-e", "-c", changes_step()["run"]], cwd=repo, env=environment, check=True, capture_output=True)
    assert output.read_text().splitlines() == [f"code={code}"]


#: How the workspaces' code runs in ci.yml: their tests, their types, and the Python core's tests,
#: which check the shared cases with the contract's validator.
CONSUMERS = ("npm test", "npm run typecheck", "pytest -q tests")
CONTRACT_BUILD = "npm run build -w @lsquare/contract"


def test_every_job_that_runs_the_workspaces_builds_the_contract_first() -> None:
    building = set()
    for job_id, job in jobs("ci.yml").items():
        runs = [str(step.get("run", "")) for step in job.get("steps", [])]
        consuming = [at for at, run in enumerate(runs) if run.endswith(CONSUMERS)]
        if not consuming:
            continue
        building.add(job_id)
        assert "npm ci" in runs and CONTRACT_BUILD in runs, job_id
        assert runs.index("npm ci") < runs.index(CONTRACT_BUILD) < min(consuming), job_id
    assert building == {"build", "tests", "python-core"}


def test_a_pull_request_has_the_contracts_version_checked_against_its_base() -> None:
    every = jobs("ci.yml")
    checking = [job_id for job_id, job in every.items() for step in job.get("steps", []) if "contract-version.mjs" in str(step.get("run", ""))]
    assert checking == ["contract-version"]
    job = every["contract-version"]
    assert job["if"] == "github.event_name == 'pull_request'"
    # A job of its own, which needs nothing and only all checks passed needs, which runs whatever the
    # jobs before it did: its failure skips no test suite.
    assert needs(job) == [] and [job_id for job_id, other in every.items() if "contract-version" in needs(other)] == ["all-checks"]
    steps = job["steps"]
    [step] = [step for step in steps if "contract-version.mjs" in str(step.get("run", ""))]
    assert step["env"] == {"BASE": "${{ github.event.pull_request.base.sha }}"}
    assert step["run"] == 'node .github/scripts/contract-version.mjs "$BASE"'
    # After the checkout of every commit, which the base is among.
    [checkout] = [at for at, other in enumerate(steps) if action(other) == "actions/checkout"]
    assert steps[checkout]["with"]["fetch-depth"] == 0 and checkout < steps.index(step)


def test_the_contracts_smoke_check_runs_on_each_node_version_it_supports() -> None:
    manifest = json.loads((WORKFLOWS.parents[1] / "packages" / "contract" / "package.json").read_text(encoding="utf-8"))
    assert manifest["engines"] == {"node": ">=20"}
    core = jobs("ci.yml")["core"]
    # Node 20, the oldest it supports, and newer ones.
    versions = core["strategy"]["matrix"]["node"]
    assert 20 in versions and max(versions) > 20
    steps = [
        step
        for step in core["steps"]
        if (step.get("with") or {}).get("name") == "lsquare-contract-package" or "packages/contract/test/smoke/run.sh" in str(step.get("run", ""))
    ]
    assert [step.get("if") for step in steps] == ["matrix.node >= 20"] * 2
    assert action(steps[0]) == "actions/download-artifact" and steps[1]["run"].endswith(f"{steps[0]['with']['path']}/lsquare-contract-*.tgz")


#: The base each pull request below changes. `bump` sets the contract's version.
CONTRACT_TREE = {
    "README.md": "x\n",
    "packages/contract/package.json": json.dumps({"name": "@lsquare/contract", "version": "0.1.0"}) + "\n",
    "packages/contract/src/index.ts": "x\n",
    "packages/contract/cases/README.md": "x\n",
    "packages/contract/REDACTION.md": "x\n",
    "packages/contract/test/cases.test.ts": "x\n",
}
BUMP = """bump() { printf '{"name": "@lsquare/contract", "version": "%s"}\\n' "$1" > packages/contract/package.json; }"""
CONTRACT_MANIFEST = "packages/contract/package.json"


@pytest.mark.parametrize(
    ("change", "failing"),
    [
        pytest.param("echo more >> README.md", None, id="the contract unchanged"),
        pytest.param("echo more >> packages/contract/src/index.ts && bump 0.1.1", None, id="changed, its version raised"),
        pytest.param("echo more >> packages/contract/src/index.ts && bump 1.0.0", None, id="changed, a major version"),
        pytest.param("bump 0.2.0", None, id="only its version raised"),
        pytest.param("echo more >> packages/contract/src/index.ts", CONTRACT_MANIFEST, id="changed, its version kept"),
        pytest.param("echo more >> packages/contract/cases/README.md", CONTRACT_MANIFEST, id="its cases' README changed, its version kept"),
        pytest.param("echo more >> packages/contract/REDACTION.md", None, id="only its redaction reference changed"),
        pytest.param("echo more >> packages/contract/test/cases.test.ts", None, id="only its own tests changed"),
        pytest.param("echo more >> packages/contract/REDACTION.md && echo more >> packages/contract/src/index.ts", CONTRACT_MANIFEST, id="its reference and its code changed, its version kept"),
        pytest.param("git mv packages/contract/src/index.ts packages/contract/src/main.ts", CONTRACT_MANIFEST, id="a file moved within it"),
        pytest.param("echo more >> packages/contract/src/index.ts && bump 0.0.9", CONTRACT_MANIFEST, id="changed, its version lowered"),
        pytest.param("echo more >> packages/contract/src/index.ts && bump 0.1.1-beta", CONTRACT_MANIFEST, id="changed, a version not x.y.z"),
        pytest.param("new", None, id="the contract new"),
        pytest.param("unknown base", CONTRACT_MANIFEST, id="a base the checkout doesn't have"),
    ],
)
def test_which_pull_requests_pass_the_contracts_version_check(tmp_path: Path, change: str, failing: str | None) -> None:
    """`failing` is the file a failure's annotation names, or None when it passes."""
    if shutil.which("node") is None:
        pytest.skip("node isn't installed: contract-version.mjs wasn't run")
    repo = tmp_path / "repo"
    for name, text in CONTRACT_TREE.items():
        if change == "new" and name.startswith("packages/contract/"):
            continue
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    base = "1" * 40 if change == "unknown base" else git(repo, "rev-parse", "HEAD")
    if change == "new":
        for name, text in CONTRACT_TREE.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(text)
    elif change != "unknown base":
        subprocess.run(["bash", "-e", "-c", f"{BUMP}\n{change}"], cwd=repo, env=isolated(repo), check=True)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", "head")
    run = subprocess.run(["node", str(CONTRACT_VERSION), base], cwd=repo, env=isolated(repo), capture_output=True, text=True)
    assert run.returncode == (0 if failing is None else 1), run.stdout + run.stderr
    annotated = [line.split("::")[1] for line in run.stdout.splitlines() if line.startswith("::error")]
    assert annotated == ([] if failing is None else [f"error file={failing}"])


def test_all_checks_passed_stands_for_every_other_job() -> None:
    every = jobs("ci.yml")
    job = every["all-checks"]
    assert job["name"] == "all checks passed"
    assert sorted(needs(job)) == sorted(set(every) - {"all-checks"})
    # Skipped along with a job that failed, it would count as passing.
    assert job["if"] == "always()"
    assert "continue-on-error" not in job
    [step] = job["steps"]
    assert step["if"] == "contains(needs.*.result, 'failure') || contains(needs.*.result, 'cancelled')"
    assert "exit 1" in step["run"]
