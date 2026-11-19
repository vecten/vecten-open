import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts/publish_target.py"
WORKFLOW_TEXT = (REPO / ".github/workflows/publish.yml").read_text()
WORKFLOW = yaml.safe_load(WORKFLOW_TEXT)
JOBS = WORKFLOW["jobs"]
PUBLISHING = {"packages", "images"}


def publish_target():
    spec = importlib.util.spec_from_file_location("publish_target", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def target(tag, version, on_main):
    return publish_target().target(tag, version, on_main)


def test_a_candidate_tag_targets_testpypi():
    assert target("v0.1.2rc1", "0.1.2rc1", on_main=False) == "candidate"
    assert target("v0.1.2rc1", "0.1.2rc1", on_main=True) == "candidate"


def test_a_release_tag_on_main_targets_pypi():
    assert target("v0.1.2", "0.1.2", on_main=True) == "release"


@pytest.mark.parametrize(
    "tag, version, on_main",
    [
        ("v0.1.2", "0.1.2", False),  # a release tag off main
        ("v0.1.2", "0.1.2rc1", True),  # the tag and the tree disagree
        ("v0.1.2rc1", "0.1.2", False),
        ("0.1.2", "0.1.2", True),  # no v
        ("v0.1.2.dev1", "0.1.2.dev1", True),  # neither a candidate nor a release
        ("v0.1", "0.1", True),
    ],
)
def test_everything_else_is_refused(tag, version, on_main):
    with pytest.raises(ValueError):
        target(tag, version, on_main)


def test_each_target_publishes_to_its_own_index():
    indexes = publish_target().INDEXES
    assert indexes == {
        "candidate": ("https://test.pypi.org/legacy/", "https://test.pypi.org/simple/"),
        "release": ("https://upload.pypi.org/legacy/", "https://pypi.org/simple/"),
    }


def git(root, *args):
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )


def checkout(tmp_path, version, on_main):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts/publish_target.py")
    versions = root / "packages/vecten-shared/src/vecten_shared/versions.toml"
    versions.parent.mkdir(parents=True)
    versions.write_text(f'vecten = "{version}"\npython = "3.14"\n')
    git(root, "init")
    git(root, "add", "--all")
    git(root, "commit", "-m", "tree")
    if on_main is None:
        return root
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    if not on_main:
        git(root, "commit", "--allow-empty", "-m", "off main")
    return root


def run_cli(root, tag):
    output = root.parent / "github_output"
    output.write_text("")
    env = {**os.environ, "GITHUB_OUTPUT": str(output)}
    result = subprocess.run(
        ["python3", "scripts/publish_target.py", tag],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    return result, output.read_text()


def test_the_cli_writes_a_release_target_for_a_release_tag_on_main(tmp_path):
    result, output = run_cli(checkout(tmp_path, "0.1.2", on_main=True), "v0.1.2")
    assert result.returncode == 0, result.stderr
    assert output.splitlines() == [
        "target=release",
        "index_url=https://upload.pypi.org/legacy/",
        "check_url=https://pypi.org/simple/",
        "version=0.1.2",
    ]


def test_the_cli_writes_a_candidate_target_off_main(tmp_path):
    result, output = run_cli(checkout(tmp_path, "0.1.2rc1", on_main=False), "v0.1.2rc1")
    assert result.returncode == 0, result.stderr
    assert "target=candidate" in output.splitlines()
    assert "index_url=https://test.pypi.org/legacy/" in output.splitlines()


@pytest.mark.parametrize(
    "version, on_main, tag, reason",
    [
        ("0.1.2", False, "v0.1.2", "not on main"),
        ("0.1.2rc1", True, "v0.1.2", "0.1.2rc1"),
        ("0.1.2", None, "v0.1.2", "git merge-base failed"),  # no origin/main to compare with
    ],
)
def test_the_cli_refuses_and_writes_nothing(tmp_path, version, on_main, tag, reason):
    result, output = run_cli(checkout(tmp_path, version, on_main), tag)
    assert result.returncode != 0
    assert reason in result.stderr
    assert output == ""


def test_the_workflow_runs_on_version_tags_only():
    assert WORKFLOW[True] == {"push": {"tags": ["v*"]}}  # PyYAML reads `on` as True


def test_a_publish_is_never_cancelled_by_another():
    assert WORKFLOW["concurrency"] == {
        "group": "publish-${{ github.ref }}",
        "cancel-in-progress": False,
    }


def uses():
    return [step["uses"] for job in JOBS.values() for step in job["steps"] if "uses" in step]


def test_every_action_is_pinned_to_a_commit():
    assert uses()
    for action in uses():
        assert re.search(r"@[0-9a-f]{40}$", action), action
    pinned = re.findall(r"uses: \S+@[0-9a-f]{40} # v\d+\.\d+\.\d+$", WORKFLOW_TEXT, re.M)
    assert len(pinned) == len(uses())


def test_the_shared_actions_carry_the_versions_table_pins():
    from vecten_shared.versions import VERSIONS

    for pin in (VERSIONS.github_actions["checkout"], VERSIONS.github_actions["setup_uv"]):
        assert f"uses: {pin}\n" in WORKFLOW_TEXT


def test_no_job_reads_a_repository_secret_but_the_token():
    assert WORKFLOW_TEXT.count("secrets") == WORKFLOW_TEXT.count("secrets.GITHUB_TOKEN") == 1
    assert "secrets.GITHUB_TOKEN" in str(JOBS["images"]["steps"])


def runs():
    return [step["run"] for job in JOBS.values() for step in job["steps"] if "run" in step]


def test_no_run_line_interpolates_an_expression():
    assert runs()
    for script in runs():
        assert "${{" not in script, script


def test_packages_publish_by_trusted_publishing_only():
    (publish,) = [s for s in runs() if "uv publish" in s]
    assert "--trusted-publishing always" in publish
    assert '--publish-url "$INDEX_URL"' in publish
    assert '--check-url "$CHECK_URL"' in publish
    assert not re.search(r"--token|--password|--username|\s-[tpu]\s", publish)
    assert not re.search(r"UV_PUBLISH_(TOKEN|PASSWORD|USERNAME)", WORKFLOW_TEXT)


def test_every_job_after_target_waits_for_it():
    assert "needs" not in JOBS["target"]
    for name, job in JOBS.items():
        if name != "target":
            assert "target" in job["needs"], name
    assert {"packages", "images"} <= set(JOBS["verify"]["needs"])
    assert "packages" in JOBS["images"]["needs"]


def test_the_target_job_refuses_through_the_script():
    steps = JOBS["target"]["steps"]
    assert steps[0]["with"]["fetch-depth"] == 0
    (step,) = [s for s in steps if "publish_target.py" in s.get("run", "")]
    assert step["env"]["TAG"] == "${{ github.ref_name }}"
    assert 'publish_target.py "$TAG"' in step["run"]
    assert set(JOBS["target"]["outputs"]) == {"target", "index_url", "check_url", "version"}


def test_only_the_packages_run_in_the_target_environment():
    assert JOBS["packages"]["environment"] == "${{ needs.target.outputs.target }}"
    for name in PUBLISHING:
        job = JOBS[name]
        assert "candidate" in job["if"] and "release" in job["if"], name
    # One approval of a release: the images run once the approved packages job has succeeded.
    assert "packages" in JOBS["images"]["needs"]
    for name in JOBS.keys() - {"packages"}:
        assert "environment" not in JOBS[name], name


RULESETS = REPO / ".github/rulesets"


def ruleset(name):
    return json.loads((RULESETS / name).read_text())


def test_release_tags_are_created_moved_and_deleted_by_the_maintainers_only():
    rules = ruleset("release-tags.json")
    assert rules["target"] == "tag" and rules["enforcement"] == "active"
    assert rules["conditions"]["ref_name"] == {
        "include": ["refs/tags/v*"],
        "exclude": ["refs/tags/v*rc*"],
    }
    assert sorted(rule["type"] for rule in rules["rules"]) == ["creation", "deletion", "update"]
    assert rules["bypass_actors"] == [
        {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
    ]


def test_candidate_tags_once_pushed_stay_where_they_are():
    rules = ruleset("candidate-tags.json")
    assert rules["target"] == "tag" and rules["enforcement"] == "active"
    assert rules["conditions"]["ref_name"] == {"include": ["refs/tags/v*rc*"], "exclude": []}
    assert sorted(rule["type"] for rule in rules["rules"]) == ["deletion", "update"]
    assert rules["bypass_actors"] == []


def test_only_the_publishing_jobs_hold_write_permissions():
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert JOBS["packages"]["permissions"] == {"contents": "read", "id-token": "write"}
    assert JOBS["images"]["permissions"] == {"contents": "read", "packages": "write"}
    for name in JOBS.keys() - PUBLISHING:
        assert "write" not in str(JOBS[name].get("permissions", {})), name


def test_images_are_pushed_to_the_published_registry_and_checked():
    from vecten.stack.up import PUBLISHED_REGISTRY

    images = "\n".join(s.get("run", "") for s in JOBS["images"]["steps"])
    assert f"uv run vecten dev build-images --push {PUBLISHED_REGISTRY} --json" in images
    assert "uv run vecten dev check-images" in images


def test_verify_installs_every_package_at_the_version():
    verify = "\n".join(s.get("run", "") for s in JOBS["verify"]["steps"])
    for name in ("vecten", "vecten-shared", "vecten-platform", "vecten-mcp"):
        assert f'"{name}==$VERSION"' in verify, name
    assert '[ "$("$venv/bin/vecten" version)" = "vecten $VERSION" ]' in verify
    assert '[ "$built" = "$published" ]' in verify


def test_publishing_jobs_restore_no_cache():
    for name in PUBLISHING:
        for step in JOBS[name]["steps"]:
            if step.get("uses", "").startswith("astral-sh/setup-uv@"):
                assert step["with"]["enable-cache"] is False, name
            assert "actions/cache" not in step.get("uses", ""), name


IMAGES_SCRIPT = REPO / "scripts/published_images.py"
NOT_FOUND = "ERROR: {ref}: not found\n"


def published_images():
    spec = importlib.util.spec_from_file_location("published_images", IMAGES_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def registry(existing, failing=None):
    def inspect(ref):
        if ref == failing:
            return 1, "ERROR: failed to authorize: 401 Unauthorized\n"
        return (0, "") if ref in existing else (1, NOT_FOUND.format(ref=ref))

    return inspect


AIRFLOW = "ghcr.io/vecten/vecten-airflow:0.1.2"
MCP = "ghcr.io/vecten/vecten-mcp:0.1.2"


@pytest.fixture
def run_guard(tmp_path, monkeypatch):
    output = tmp_path / "github_output"
    output.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    def run(inspect):
        return published_images().main(["ghcr.io/vecten", "0.1.2"], inspect), output.read_text()

    return run


def test_the_guard_names_the_refs_check_images_inspects():
    from vecten.stack.images import base_image

    expected = [base_image(kind, "ghcr.io/vecten", "0.1.2") for kind in ("airflow", "mcp")]
    assert published_images().refs("ghcr.io/vecten", "0.1.2") == expected == [AIRFLOW, MCP]


def test_a_version_with_no_images_is_built(run_guard):
    assert run_guard(registry(set())) == (0, "build=true\n")


def test_a_version_with_both_images_is_only_checked(run_guard, capsys):
    assert run_guard(registry({AIRFLOW, MCP})) == (0, "build=false\n")
    assert "skipping the build" in capsys.readouterr().out


@pytest.mark.parametrize("existing, absent", [(AIRFLOW, MCP), (MCP, AIRFLOW)])
def test_a_version_with_one_image_is_left_to_a_maintainer(run_guard, capsys, existing, absent):
    assert run_guard(registry({existing})) == (1, "")
    message = capsys.readouterr().err
    assert f"{existing} exists but {absent} does not" in message
    assert "maintainer" in message


def test_a_registry_that_cannot_answer_is_not_read_as_missing(run_guard, capsys):
    assert run_guard(registry(set(), failing=MCP)) == (1, "")
    assert "401 Unauthorized" in capsys.readouterr().err


def test_the_registry_error_code_for_a_missing_tag_reads_as_missing():
    assert published_images().missing(MCP, 1, "ERROR: MANIFEST_UNKNOWN: manifest unknown")


def test_the_build_runs_only_when_the_guard_says_so():
    steps = JOBS["images"]["steps"]
    (guard,) = [s for s in steps if "published_images.py" in s.get("run", "")]
    assert guard["id"] == "published"
    assert guard["env"]["VERSION"] == "${{ needs.target.outputs.version }}"
    assert guard["run"] == 'python3 scripts/published_images.py ghcr.io/vecten "$VERSION"'
    (build,) = [s for s in steps if "build-images" in s.get("run", "")]
    assert build["if"] == "steps.published.outputs.build == 'true'"
    assert steps.index(guard) < steps.index(build)
    (check,) = [s for s in steps if "check-images" in s.get("run", "")]
    assert "if" not in check


def test_verify_waits_for_each_published_hash():
    (compare,) = [s["run"] for s in JOBS["verify"]["steps"] if "sha256sum" in s.get("run", "")]
    assert "deadline=$((SECONDS + 300))" in compare
    assert '&& [ -n "$published" ]; do' in compare
