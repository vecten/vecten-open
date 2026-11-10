"""The rendered deploy workflows, parsed: gates, roles, environments, concurrency, plan comment."""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from vecten.scaffold.render import render_project
from vecten_shared.versions import VERSIONS

from .scaffold_matrix import MATRIX, label

WORKFLOWS = ["terraform-plan.yml", "ship.yml", "terraform-apply.yml", "deploy-platform.yml"]
AWS = [p for p in MATRIX if p.render_deploy and p.cloud == "aws"]
SCALEWAY = [p for p in MATRIX if p.render_deploy and p.cloud == "scaleway"]
DEPLOYED = AWS + SCALEWAY
CREDENTIALS_ACTION = "aws-actions/configure-aws-credentials"
GITHUB_COMMENT_LIMIT = 65536
LONG_LIVED_KEYS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "aws-access-key-id",
    "secret-access-key",
    "secrets.AWS_",
)


@pytest.fixture(scope="module")
def renders(tmp_path_factory):
    """Each parameter set rendered once per module; tests only read the workflow files."""
    cache: dict[str, Path] = {}

    def _render(params) -> Path:
        key = label(params)
        if key not in cache:
            target = tmp_path_factory.mktemp("wf") / "proj"
            render_project(target, params)
            cache[key] = target / ".github" / "workflows"
        return cache[key]

    return _render


@pytest.fixture
def render(renders):
    def _docs(params) -> dict[str, dict]:
        wf = renders(params)
        return {name: yaml.safe_load((wf / name).read_text()) for name in WORKFLOWS}

    return _docs


@pytest.fixture
def raw(renders):
    def _text(params, name: str) -> str:
        return (renders(params) / name).read_text()

    return _text


def triggers(doc: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True.
    return doc[True] if True in doc else doc["on"]


def steps(job: dict) -> list[dict]:
    return job.get("steps", [])


def runs(job: dict) -> list[str]:
    return [s["run"] for s in steps(job) if "run" in s]


def credential_steps(job: dict) -> list[dict]:
    return [s for s in steps(job) if s.get("uses", "").startswith(CREDENTIALS_ACTION)]


def permissions(doc: dict, job: dict) -> dict:
    # A job's own permissions replace the workflow's entirely.
    return job["permissions"] if "permissions" in job else doc.get("permissions", {})


# Gates: nothing touches the cloud before bootstrap or before the first deploy


@pytest.mark.parametrize("params", DEPLOYED, ids=label)
def test_workflows_gate_on_vecten_toml_facts(render, params):
    docs = render(params)

    plan = docs["terraform-plan.yml"]["jobs"]
    gate = plan["gate"]
    [check] = [s for s in steps(gate) if s.get("run") == "vecten deploy --env prod --check"]
    assert gate["outputs"]["bootstrapped"] == f"${{{{ steps.{check['id']}.outputs.bootstrapped }}}}"
    assert plan["plan"]["needs"] == "gate"
    assert plan["plan"]["if"] == "needs.gate.outputs.bootstrapped == 'true'"

    ship = docs["ship.yml"]["jobs"]
    gate = ship["gate"]
    [check] = [s for s in steps(gate) if s.get("run") == "vecten ship --env prod --check"]
    assert gate["outputs"]["deployed"] == f"${{{{ steps.{check['id']}.outputs.deployed }}}}"
    assert ship["ship"]["needs"] == "gate"
    assert ship["ship"]["if"] == "needs.gate.outputs.deployed == 'true'"
    assert ship["ship"]["uses"] == "./.github/workflows/deploy-platform.yml"
    assert ship["ship"]["with"] == {"environment": "prod"}

    for name in ("terraform-plan.yml", "ship.yml"):
        gate = docs[name]["jobs"]["gate"]
        # The facts come from vecten.toml alone: no credentials, no token, no environment.
        assert credential_steps(gate) == [], name
        assert "environment" not in gate, name
        assert permissions(docs[name], gate) == {"contents": "read"}, name
        assert not any("secrets." in str(s) for s in steps(gate)), name


# The plan workflow


@pytest.mark.parametrize("params", AWS, ids=label)
def test_plan_job_assumes_the_plan_role_without_an_environment(render, params):
    doc = render(params)["terraform-plan.yml"]
    job = doc["jobs"]["plan"]
    assert "environment" not in job
    [creds] = credential_steps(job)
    assert creds["with"]["role-to-assume"] == "${{ vars.VECTEN_AWS_PLAN_ROLE_ARN_PROD }}"
    assert creds["with"]["aws-region"] == params.region
    assert "role-duration-seconds" not in creds["with"]
    assert permissions(doc, job)["id-token"] == "write"
    assert permissions(doc, job)["pull-requests"] == "write"
    assert "vecten deploy --env prod --plan-only --cloud-only --json" in runs(job)
    assert set(triggers(doc)) == {"pull_request", "push"}


@pytest.mark.parametrize("params", DEPLOYED[:1] + SCALEWAY[:1], ids=label)
def test_plan_comment_is_fenced_and_truncated_under_the_limit(render, params, tmp_path):
    doc = render(params)["terraform-plan.yml"]
    job = doc["jobs"]["plan"]
    [comment] = [s for s in steps(job) if "gh pr comment" in s.get("run", "")]
    assert comment["if"] == "github.event_name == 'pull_request'"
    assert comment["env"]["GH_TOKEN"] == "${{ github.token }}"
    script = comment["run"]
    assert "${{ github.event.pull_request.number }}" in script
    assert "--body-file" in script

    work = tmp_path / "work"
    plans = work / ".vecten" / "plans"
    plans.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        '#!/bin/sh\necho "$@" > "$GH_ARGS"\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--body-file" ]; then cp "$2" "$GH_BODY"; fi\n'
        "  shift\n"
        "done\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "GH_ARGS": str(tmp_path / "args"),
        "GH_BODY": str(tmp_path / "body"),
        "GH_TOKEN": "x",
    }
    concrete = script.replace("${{ github.event.pull_request.number }}", "7")

    for size, truncated in ((2_000, False), (200_000, True)):
        (plans / "prod.txt").write_text("+ resource line\n" * (size // 16))
        result = subprocess.run(
            ["bash", "-e", "-c", concrete], cwd=work, env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        body = (tmp_path / "body").read_text()
        assert len(body) < GITHUB_COMMENT_LIMIT
        fences = [ln for ln in body.splitlines() if re.fullmatch(r"`{3,}(diff)?", ln)]
        assert len(fences) == 2, body[:200]
        assert fences[0].startswith(fences[1]) and fences[0].endswith("diff")
        assert ("truncated" in body) is truncated
        assert "+ resource line" in body
        assert (tmp_path / "args").read_text().split()[:3] == ["pr", "comment", "7"]


# Apply and deploy-platform


@pytest.mark.parametrize("params", AWS, ids=label)
def test_apply_and_deploy_platform_declare_the_environment(render, params):
    docs = render(params)
    for name, job_name, command in (
        ("terraform-apply.yml", "apply", "vecten deploy --env ${{ inputs.environment }} --json"),
        ("deploy-platform.yml", "ship", "vecten ship --env ${{ inputs.environment }} --json"),
    ):
        doc = docs[name]
        job = doc["jobs"][job_name]
        assert job["environment"] == "${{ inputs.environment }}", name
        [creds] = credential_steps(job)
        assert creds["with"]["role-to-assume"] == "${{ vars.VECTEN_AWS_ROLE_ARN }}", name
        assert creds["with"]["aws-region"] == params.region, name
        # The deploy role keeps IAM's default one-hour session.
        assert int(creds["with"].get("role-duration-seconds", 3600)) <= 3600, name
        assert permissions(doc, job)["id-token"] == "write", name
        assert command in runs(job), name


@pytest.mark.parametrize("params", DEPLOYED, ids=label)
def test_concurrency_is_one_group_per_environment(render, params):
    docs = render(params)
    apply = docs["terraform-apply.yml"]
    assert apply["concurrency"] == {
        "group": "deploy-${{ inputs.environment }}",
        "cancel-in-progress": False,
    }
    platform = docs["deploy-platform.yml"]
    assert "concurrency" not in platform
    assert platform["jobs"]["ship"]["concurrency"] == {
        "group": "deploy-${{ inputs.environment }}",
        "cancel-in-progress": False,
    }
    # The called workflow's job holds the group; one here too would wait on itself.
    assert "concurrency" not in docs["ship.yml"]
    assert "concurrency" not in docs["ship.yml"]["jobs"]["ship"]


@pytest.mark.parametrize("params", AWS, ids=label)
def test_every_role_assuming_job_may_request_an_oidc_token(render, params):
    docs = render(params)
    assumed = 0
    for name, doc in docs.items():
        for job_name, job in doc["jobs"].items():
            if credential_steps(job):
                assumed += 1
                assert permissions(doc, job).get("id-token") == "write", (name, job_name)
    assert assumed == 3
    # A reusable workflow's token is capped by the calling job's permissions.
    caller = docs["ship.yml"]["jobs"]["ship"]
    assert permissions(docs["ship.yml"], caller).get("id-token") == "write"


@pytest.mark.parametrize("params", AWS, ids=label)
def test_aws_renders_hold_no_long_lived_key(raw, params):
    for name in WORKFLOWS:
        text = raw(params, name)
        for key in LONG_LIVED_KEYS:
            assert key not in text, (name, key)


@pytest.mark.parametrize("params", DEPLOYED, ids=label)
def test_no_push_trigger_leads_to_an_apply(render, params):
    docs = render(params)
    for name, doc in docs.items():
        if "push" not in triggers(doc):
            continue
        for job_name, job in doc["jobs"].items():
            for line in runs(job):
                if line.startswith("vecten deploy"):
                    assert "--plan-only" in line or "--check" in line, (name, job_name, line)
            if "uses" in job:
                called = docs[Path(job["uses"]).name]
                assert set(triggers(called)) <= {"workflow_call", "workflow_dispatch"}
                for called_job in called["jobs"].values():
                    assert not any(ln.startswith("vecten deploy") for ln in runs(called_job))
    assert set(triggers(docs["terraform-apply.yml"])) == {"workflow_dispatch"}


@pytest.mark.parametrize("params", DEPLOYED, ids=label)
def test_terraform_runs_without_the_output_wrapper(render, params):
    docs = render(params)
    seen = 0
    for name, doc in docs.items():
        for job in doc["jobs"].values():
            for step in steps(job):
                if step.get("uses", "").startswith("hashicorp/setup-terraform"):
                    seen += 1
                    assert step["with"]["terraform_wrapper"] is False, name
    assert seen == 2


# Scaleway: scoped API keys from GitHub secrets, no OIDC


def secret_ref(name: str) -> str:
    return f"${{{{ secrets.{name} }}}}"


def vecten_step(job: dict, command: str) -> dict:
    [step] = [s for s in steps(job) if s.get("run") == command]
    return step


def key_env(access: str, secret: str) -> dict:
    return {"SCW_ACCESS_KEY": secret_ref(access), "SCW_SECRET_KEY": secret_ref(secret)}


SCALEWAY_JOBS = (
    ("terraform-plan.yml", "plan", "vecten deploy --env prod --plan-only --cloud-only --json"),
    ("terraform-apply.yml", "apply", "vecten deploy --env ${{ inputs.environment }} --json"),
    ("deploy-platform.yml", "ship", "vecten ship --env ${{ inputs.environment }} --json"),
)


@pytest.mark.parametrize("params", SCALEWAY, ids=label)
def test_scaleway_plan_job_signs_with_the_plan_key_without_an_environment(render, params):
    from vecten.deploy.scaleway.bootstrap import plan_secret_names

    doc = render(params)["terraform-plan.yml"]
    job = doc["jobs"]["plan"]
    # A pull request's code runs here, so the job reaches no environment secret.
    assert "environment" not in job
    assert credential_steps(job) == []
    step = vecten_step(job, "vecten deploy --env prod --plan-only --cloud-only --json")
    assert step["env"] == key_env(*plan_secret_names("prod"))
    assert permissions(doc, job) == {"contents": "read", "pull-requests": "write"}
    assert set(triggers(doc)) == {"pull_request", "push"}


@pytest.mark.parametrize("params", SCALEWAY, ids=label)
def test_scaleway_apply_and_ship_sign_with_the_environment_deploy_key(render, params):
    from vecten.deploy.scaleway.bootstrap import ENV_ACCESS_SECRET, ENV_SECRET_SECRET

    docs = render(params)
    for name, job_name, command in SCALEWAY_JOBS[1:]:
        doc = docs[name]
        job = doc["jobs"][job_name]
        # Environment secrets reach only a job that declares the environment.
        assert job["environment"] == "${{ inputs.environment }}", name
        assert credential_steps(job) == [], name
        assert vecten_step(job, command)["env"] == {
            **key_env(ENV_ACCESS_SECRET, ENV_SECRET_SECRET),
            "GH_TOKEN": "${{ github.token }}",
        }, name
        assert permissions(doc, job) == {"contents": "read", "pull-requests": "read"}, name


@pytest.mark.parametrize("params", SCALEWAY, ids=label)
def test_scaleway_jobs_request_no_oidc_token_and_pass_no_secrets_down(render, params):
    docs = render(params)
    for name, doc in docs.items():
        assert "id-token" not in doc.get("permissions", {}), name
        for job_name, job in doc["jobs"].items():
            assert "id-token" not in permissions(doc, job), (name, job_name)


@pytest.mark.parametrize("params", SCALEWAY, ids=label)
def test_scaleway_ship_passes_the_deploy_key_by_name(render, params):
    from vecten.deploy.scaleway.bootstrap import ENV_ACCESS_SECRET, ENV_SECRET_SECRET

    docs = render(params)
    # A called workflow gets an environment secret only when its caller passes it by name;
    # passing by name, not inherit, keeps the repository's plan keys out of the ship job.
    assert docs["ship.yml"]["jobs"]["ship"]["secrets"] == {
        ENV_ACCESS_SECRET: secret_ref(ENV_ACCESS_SECRET),
        ENV_SECRET_SECRET: secret_ref(ENV_SECRET_SECRET),
    }
    declared = triggers(docs["deploy-platform.yml"])["workflow_call"]["secrets"]
    assert declared == {
        ENV_ACCESS_SECRET: {"required": True},
        ENV_SECRET_SECRET: {"required": True},
    }


@pytest.mark.parametrize("params", DEPLOYED, ids=label)
def test_an_aws_ship_passes_no_secret(render, params):
    if params.cloud != "aws":
        pytest.skip("AWS signs with OIDC")
    docs = render(params)
    assert "secrets" not in docs["ship.yml"]["jobs"]["ship"]
    assert "secrets" not in triggers(docs["deploy-platform.yml"])["workflow_call"]


@pytest.mark.parametrize("params", SCALEWAY, ids=label)
def test_scaleway_keys_reach_only_the_vecten_step(render, raw, params):
    docs = render(params)
    wanted = {(name, job_name, command) for name, job_name, command in SCALEWAY_JOBS}
    for name, doc in docs.items():
        assert "GITHUB_ENV" not in raw(params, name), name
        for job_name, job in doc["jobs"].items():
            for step in steps(job):
                if "secrets." not in str(step):
                    continue
                # The comment step reads only the workflow's own token.
                assert (name, job_name, step.get("run")) in wanted, (name, job_name, step)
                assert "secrets." not in step["run"], (name, job_name)


@pytest.mark.parametrize("params", DEPLOYED, ids=label)
def test_each_cloud_renders_none_of_the_other_clouds_credentials(raw, params):
    other = ("SCW_", "scw") if params.cloud == "aws" else ("VECTEN_AWS", CREDENTIALS_ACTION)
    for name in WORKFLOWS:
        text = raw(params, name)
        for marker in other:
            assert marker not in text, (name, marker)


@pytest.mark.parametrize("params", DEPLOYED, ids=label)
def test_ship_jobs_can_read_review_verification(render, params):
    docs = render(params)
    for name in ("ship.yml", "terraform-apply.yml", "deploy-platform.yml"):
        for job in docs[name]["jobs"].values():
            if any(
                ("vecten ship" in cmd or "vecten deploy" in cmd) and "--check" not in cmd
                for cmd in runs(job)
            ) or job.get("uses", "").endswith("deploy-platform.yml"):
                assert permissions(docs[name], job).get("pull-requests") == "read"


# Actions and runners: every rendered workflow takes them from the versions table


def _pinned_actions() -> set[str]:
    return {uses.split(" #", 1)[0] for uses in VERSIONS.github_actions.values()}


@pytest.mark.parametrize("params", DEPLOYED, ids=label)
def test_deploy_workflows_pin_the_table_actions_and_runner(render, raw, params):
    for name, doc in render(params).items():
        for job in doc["jobs"].values():
            if "uses" in job:
                continue
            assert job["runs-on"] == VERSIONS.github_runner, name
            for step in steps(job):
                if "uses" in step:
                    assert step["uses"] in _pinned_actions(), (name, step["uses"])
        for line in raw(params, name).splitlines():
            if "uses:" in line and "@" in line:
                assert line.split("uses:", 1)[1].strip() in VERSIONS.github_actions.values()


@pytest.mark.parametrize("params", MATRIX[:1], ids=label)
def test_project_ci_pins_the_table_actions_and_runner(renders, params):
    doc = yaml.safe_load((renders(params) / "ci.yml").read_text())
    for job in doc["jobs"].values():
        assert job["runs-on"] == VERSIONS.github_runner
        for step in steps(job):
            if "uses" in step:
                assert step["uses"] in _pinned_actions(), step["uses"]
