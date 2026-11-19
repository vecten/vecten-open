import importlib.util
import json
import os
import re
import shutil
import subprocess
import tomllib
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
    versions.write_text(
        f'vecten = "{version}"\npython = "3.14"\n\n[images]\n'
        'airflow = "apache/airflow:9.8.7-python3.14"\nmcp_base = "python:3.14-slim"\n'
    )
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
        "airflow_image=apache/airflow:9.8.7-python3.14",
        "airflow_version=9.8.7",
        "mcp_base_image=python:3.14-slim",
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
    assert {"packages", "published", "images"} <= set(JOBS["verify"]["needs"])
    assert {"packages", "published"} <= set(JOBS["images"]["needs"])


def test_the_target_job_refuses_through_the_script():
    steps = JOBS["target"]["steps"]
    assert steps[0]["with"]["fetch-depth"] == 0
    (step,) = [s for s in steps if "publish_target.py" in s.get("run", "")]
    assert step["env"]["TAG"] == "${{ github.ref_name }}"
    assert 'publish_target.py "$TAG"' in step["run"]
    assert set(JOBS["target"]["outputs"]) == {
        "target",
        "index_url",
        "check_url",
        "version",
        *IMAGE_ARGS,
    }
    for name, value in JOBS["target"]["outputs"].items():
        assert value == f"${{{{ steps.target.outputs.{name} }}}}"


# Each image build argument the target job computes: its output name, and the argument's.
IMAGE_ARGS = {
    "airflow_image": "AIRFLOW_IMAGE",
    "airflow_version": "AIRFLOW_VERSION",
    "mcp_base_image": "BASE_IMAGE",
}
VERSIONS_TABLE = REPO / "packages/vecten-shared/src/vecten_shared/versions.toml"


def test_the_target_scripts_image_arguments_are_the_versions_tables():
    from vecten_shared.versions import VERSIONS

    assert publish_target().image_args(tomllib.loads(VERSIONS_TABLE.read_text())) == {
        "airflow_image": VERSIONS.airflow_image,
        "airflow_version": VERSIONS.airflow_version,
        "mcp_base_image": VERSIONS.mcp_base_image,
    }


def test_only_the_packages_run_in_the_target_environment():
    assert JOBS["packages"]["environment"] == "${{ needs.target.outputs.target }}"
    for name in PUBLISHING | {"published"}:
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
    assert JOBS["published"]["permissions"] == {"contents": "read"}
    for name in JOBS.keys() - PUBLISHING:
        assert "write" not in str(JOBS[name].get("permissions", {})), name
    writers = [n for n, j in JOBS.items() if j.get("permissions", {}).get("packages") == "write"]
    assert writers == ["images"]


def build_pushes():
    steps = JOBS["images"]["steps"]
    return [s for s in steps if s.get("uses", "").startswith("docker/build-push-action@")]


def build_args(step):
    return dict(line.split("=", 1) for line in step["with"]["build-args"].splitlines())


REF_VERSION = "${{ steps.version.outputs.version }}"


def test_images_are_pushed_to_the_published_registry_under_the_refs_version():
    from vecten.stack.images import REQUIRED_PLATFORMS
    from vecten.stack.up import PUBLISHED_REGISTRY

    airflow, mcp = build_pushes()
    for step, name in ((airflow, "vecten-airflow"), (mcp, "vecten-mcp")):
        assert step["with"]["tags"] == f"{PUBLISHED_REGISTRY}/{name}:{REF_VERSION}"
        assert step["with"]["push"] is True
        assert step["with"]["context"] == "."
        assert step["with"]["platforms"] == ",".join(REQUIRED_PLATFORMS)
        assert build_args(step)["VECTEN_VERSION"] == REF_VERSION
    assert airflow["with"]["file"] == "images/airflow/Dockerfile"
    assert mcp["with"]["file"] == "images/mcp/Dockerfile"


def test_a_build_push_takes_no_input_beyond_the_image_it_builds():
    for step in build_pushes():
        assert set(step["with"]) == {"context", "file", "platforms", "push", "tags", "build-args"}


def test_the_build_arguments_are_those_build_images_passes(invoke):
    result = invoke("dev", "build-images", "--print", "--json", "--repo", str(REPO))
    assert result.exit_code == 0, result.stdout
    _, *commands = json.loads(result.stdout)["commands"]
    computed = publish_target().image_args(tomllib.loads(VERSIONS_TABLE.read_text()))
    for command, step in zip(commands, build_pushes(), strict=True):
        assert command[command.index("-f") + 1] == str(REPO / step["with"]["file"])
        passed = dict(
            command[i + 1].split("=", 1) for i, arg in enumerate(command) if arg == "--build-arg"
        )
        args = build_args(step)
        assert args.keys() == passed.keys()
        for name, value in args.items():
            if name == "VECTEN_VERSION":
                continue
            (output,) = [o for o, arg in IMAGE_ARGS.items() if arg == name]
            assert value == f"${{{{ needs.target.outputs.{output} }}}}"
            assert computed[output] == passed[name]


# The job that can push images runs these actions, by these commits, and no other step but the
# one shell step below; a change to either is a change to what holds the registry credential.
IMAGE_JOB_ACTIONS = [
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333",
    None,
    "docker/setup-qemu-action@99012661954931238ded8c8b007157a8430204e1",
    "docker/setup-buildx-action@f87e5991a6d7451dcb8d9637bfbc97413f497069",
    "docker/login-action@8aa5831fb8d48d8f76594c7ddde61ae58019f550",
    "docker/build-push-action@c3c9e263c25d99ce0380d002d59b67737d91b0dc",
    "docker/build-push-action@c3c9e263c25d99ce0380d002d59b67737d91b0dc",
]
IMAGE_JOB_SHELL = """\
pattern='^v[0-9]+\\.[0-9]+\\.[0-9]+(rc[0-9]+)?$'
if ! [[ $REF_NAME =~ $pattern ]]; then
  echo "refused: the ref $REF_NAME is not vX.Y.Z or vX.Y.ZrcN" >&2
  exit 1
fi
echo "version=${REF_NAME#v}" >> "$GITHUB_OUTPUT"
"""


def test_the_image_job_runs_only_its_pinned_actions_and_its_own_shell():
    steps = JOBS["images"]["steps"]
    assert [s.get("uses") for s in steps] == IMAGE_JOB_ACTIONS
    (shell,) = [s for s in steps if "uses" not in s]
    assert shell == {
        "id": "version",
        "shell": "bash",
        "env": {"REF_NAME": "${{ github.ref_name }}"},
        "run": IMAGE_JOB_SHELL,
    }
    for step in steps:
        if "uses" in step:
            assert "run" not in step


def test_no_step_of_the_image_job_runs_after_a_failure():
    airflow, mcp = build_pushes()
    steps = JOBS["images"]["steps"]
    assert steps.index(airflow) < steps.index(mcp)
    assert airflow["with"]["file"] == "images/airflow/Dockerfile"
    for step in steps:
        assert "if" not in step and "continue-on-error" not in step, step


def test_the_image_job_leaves_no_token_in_the_build_context():
    (checkout,) = [s for s in JOBS["images"]["steps"] if "actions/checkout@" in s.get("uses", "")]
    assert checkout["with"] == {"persist-credentials": False}


def test_the_images_are_built_from_the_wheels_the_packages_job_published():
    (build,) = [s["run"] for s in JOBS["packages"]["steps"] if "uv build" in s.get("run", "")]
    assert "--sdist" not in build and "--out-dir dist" in build
    (upload,) = [s for s in JOBS["packages"]["steps"] if "upload-artifact@" in s.get("uses", "")]
    (download,) = [s for s in JOBS["images"]["steps"] if "download-artifact@" in s.get("uses", "")]
    assert upload["with"]["path"] == "dist/"
    assert download["with"] == {"name": upload["with"]["name"], "path": "dist"}
    for image in ("airflow", "mcp"):
        dockerfile = (REPO / f"images/{image}/Dockerfile").read_text()
        assert "source=dist,target=/mnt/wheels" in dockerfile


def version_step():
    (step,) = [s for s in JOBS["images"]["steps"] if s.get("id") == "version"]
    return step


def run_version_step(tmp_path, ref_name):
    script = tmp_path / "version.sh"
    script.write_text(version_step()["run"])
    output = tmp_path / "github_output"
    output.write_text("")
    env = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(output), "REF_NAME": ref_name}
    # The runner's command line for `shell: bash`.
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        env=env,
        capture_output=True,
        text=True,
    )
    return result, output.read_text()


def test_the_image_version_comes_from_the_pushed_ref_through_env():
    step = version_step()
    assert step["env"] == {"REF_NAME": "${{ github.ref_name }}"}
    assert "$GITHUB_OUTPUT" in step["run"]


@pytest.mark.parametrize("ref_name, version", [("v0.1.2rc1", "0.1.2rc1"), ("v0.1.2", "0.1.2")])
def test_the_version_step_drops_the_v(tmp_path, ref_name, version):
    result, output = run_version_step(tmp_path, ref_name)
    assert result.returncode == 0, result.stderr
    assert output == f"version={version}\n"


@pytest.mark.parametrize(
    "ref_name",
    ["0.1.2", "vfoo", "", "v", "v0.1.2.dev1", "v0.1.2\nversion=0.1.1", "v0.1.2rc1-x"],
)
def test_the_version_step_refuses_any_other_ref(tmp_path, ref_name):
    result, output = run_version_step(tmp_path, ref_name)
    assert result.returncode != 0
    assert output == ""
    assert "refused" in result.stderr


def test_verify_checks_both_images_with_the_published_cli():
    steps = JOBS["verify"]["steps"]
    (check,) = [s for s in steps if "check-images" in s.get("run", "")]
    assert check["env"] == {"VERSION": "${{ needs.target.outputs.version }}"}
    assert check["run"].strip() == (
        '"$RUNNER_TEMP/published/bin/vecten" dev check-images --version "$VERSION"'
    )
    (install,) = [s for s in steps if "uv pip install" in s.get("run", "")]
    assert steps.index(install) < steps.index(check)


def test_verify_runs_after_a_skipped_image_build_and_never_after_a_failed_one():
    verify = JOBS["verify"]
    assert set(verify["needs"]) == {"target", "packages", "published", "images"}
    assert " ".join(verify["if"].split()) == (
        "!cancelled() && needs.packages.result == 'success'"
        " && needs.published.result == 'success'"
        " && (needs.images.result == 'success'"
        " || (needs.images.result == 'skipped' && needs.published.outputs.build == 'false'))"
    )


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
    published = JOBS["published"]
    assert published["needs"] == "target"
    assert published["if"] == JOBS["packages"]["if"]
    (guard,) = [s for s in published["steps"] if "published_images.py" in s.get("run", "")]
    assert guard["id"] == "published"
    assert guard["env"]["VERSION"] == "${{ needs.target.outputs.version }}"
    assert guard["run"] == 'python3 scripts/published_images.py ghcr.io/vecten "$VERSION"'
    assert published["outputs"] == {"build": "${{ steps.published.outputs.build }}"}
    assert JOBS["images"]["if"] == (
        f"{JOBS['packages']['if']} && needs.published.outputs.build == 'true'"
    )


def test_verify_waits_for_each_published_hash():
    (compare,) = [s["run"] for s in JOBS["verify"]["steps"] if "sha256sum" in s.get("run", "")]
    assert "deadline=$((SECONDS + 300))" in compare
    assert '&& [ -n "$published" ]; do' in compare
