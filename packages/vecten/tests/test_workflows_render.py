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
    # Without --env the gate reports the first environment of the promotion order, read from
    # the pushed vecten.toml when the workflow runs.
    [check] = [s for s in steps(gate) if s.get("run") == "vecten ship --check"]
    assert gate["outputs"]["deployed"] == f"${{{{ steps.{check['id']}.outputs.deployed }}}}"
    assert gate["outputs"]["env"] == f"${{{{ steps.{check['id']}.outputs.env }}}}"
    assert ship["ship"]["needs"] == "gate"
    assert ship["ship"]["if"] == "needs.gate.outputs.deployed == 'true'"
    assert ship["ship"]["uses"] == "./.github/workflows/deploy-platform.yml"
    assert ship["ship"]["with"] == {
        "environment": "${{ needs.gate.outputs.env }}",
        "commit": "${{ github.sha }}",
    }

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
        ("terraform-apply.yml", "apply", APPLY_COMMAND),
        ("deploy-platform.yml", "ship", SHIP_COMMAND),
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


APPLY_COMMAND = 'vecten deploy --env "$ENVIRONMENT" ${COMMIT:+--commit "$COMMIT"} --json'
SHIP_COMMAND = 'vecten ship --env "$ENVIRONMENT" ${COMMIT:+--commit "$COMMIT"} --json'
SCALEWAY_JOBS = (
    ("terraform-plan.yml", "plan", "vecten deploy --env prod --plan-only --cloud-only --json"),
    ("terraform-apply.yml", "apply", APPLY_COMMAND),
    ("deploy-platform.yml", "ship", SHIP_COMMAND),
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
            "GIT_CREDENTIALS": GIT_CREDENTIALS_VALUE,
            "COMMIT": "${{ inputs.commit }}",
            "ENVIRONMENT": "${{ inputs.environment }}",
        }, name
        assert permissions(doc, job) == {
            "contents": "read",
            "pull-requests": "read",
            "statuses": "write",
        }, name


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
        **{name: secret_ref(name) for name in GIT_SECRETS},
    }
    declared = triggers(docs["deploy-platform.yml"])["workflow_call"]["secrets"]
    assert declared == {
        ENV_ACCESS_SECRET: {"required": True},
        ENV_SECRET_SECRET: {"required": True},
        **{name: {"required": False} for name in GIT_SECRETS},
    }


@pytest.mark.parametrize("params", AWS, ids=label)
def test_an_aws_ship_passes_only_the_optional_git_secrets(render, params):
    docs = render(params)
    # AWS signs with OIDC, so the git credential is the only secret the ship job needs.
    assert docs["ship.yml"]["jobs"]["ship"]["secrets"] == {
        name: secret_ref(name) for name in GIT_SECRETS
    }
    assert triggers(docs["deploy-platform.yml"])["workflow_call"]["secrets"] == {
        name: {"required": False} for name in GIT_SECRETS
    }


@pytest.mark.parametrize("params", SCALEWAY, ids=label)
def test_scaleway_keys_reach_only_the_vecten_step(render, raw, params):
    docs = render(params)
    wanted = {(name, job_name, command) for name, job_name, command in SCALEWAY_JOBS}
    for name, doc in docs.items():
        assert "GITHUB_ENV" not in raw(params, name), name
        for job_name, job in doc["jobs"].items():
            for step in steps(job):
                if "secrets." not in str(step) or step.get("id") == APP_TOKEN_STEP:
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


# The git credential: only the step that runs vecten ship, or vecten deploy, gets it

GIT_SECRETS = ("VECTEN_GIT_CREDENTIALS", "VECTEN_GIT_APP_PRIVATE_KEY")
APP_TOKEN_STEP = "git-app-token"
# Both the minted token and the secret's entries, so a GitHub App and a token for another
# host can serve one project together.
GIT_CREDENTIALS_VALUE = (
    "${{ format('{0} {1}', steps.git-app-token.outputs.token && "
    "format('https://x-access-token:{0}@github.com', steps.git-app-token.outputs.token) "
    "|| '', secrets.VECTEN_GIT_CREDENTIALS) }}"
)
APP_TOKEN_CONDITION = (
    "vars.VECTEN_GIT_APP_ID != '' && vars.VECTEN_GIT_APP_OWNER != '' && "
    "vars.VECTEN_GIT_APP_REPOSITORIES != '' && env.VECTEN_GIT_APP_KEY_SET == 'true'"
)
SHIPPING_JOBS = (
    (
        "terraform-apply.yml",
        "apply",
        'vecten deploy --env "$ENVIRONMENT" ${COMMIT:+--commit "$COMMIT"} --json',
    ),
    (
        "deploy-platform.yml",
        "ship",
        'vecten ship --env "$ENVIRONMENT" ${COMMIT:+--commit "$COMMIT"} --json',
    ),
)


@pytest.mark.parametrize("params", DEPLOYED, ids=label)
def test_only_the_shipping_vecten_step_gets_the_git_credential(render, raw, params):
    docs = render(params)
    wanted = {(name, job, command) for name, job, command in SHIPPING_JOBS}
    holders = set()
    for name, doc in docs.items():
        assert "GIT_CREDENTIALS" not in str(doc.get("env", {})), name
        for job_name, job in doc["jobs"].items():
            assert "GIT_CREDENTIALS" not in str(job.get("env", {})), (name, job_name)
            for step in steps(job):
                if "GIT_CREDENTIALS" in str(step.get("env", {})) or "app-token" in str(
                    step.get("env", {})
                ):
                    holders.add((name, job_name, step.get("run")))
                if step.get("id") != APP_TOKEN_STEP:
                    assert "VECTEN_GIT_APP_PRIVATE_KEY" not in str(step), (name, job_name)
    assert holders == wanted
    for name, job_name, command in SHIPPING_JOBS:
        env = vecten_step(docs[name]["jobs"][job_name], command)["env"]
        assert env["GIT_CREDENTIALS"] == GIT_CREDENTIALS_VALUE, name
    # A pull request's code runs in the plan job, which never reaches the git credential.
    assert "GIT_" not in raw(params, "terraform-plan.yml")


@pytest.mark.parametrize("params", DEPLOYED, ids=label)
def test_a_github_app_token_is_minted_for_the_shipping_job_only_when_configured(render, params):
    docs = render(params)
    for name, job_name, command in SHIPPING_JOBS:
        job = docs[name]["jobs"][job_name]
        ids = [step.get("id") for step in steps(job)]
        [mint] = [step for step in steps(job) if step.get("id") == APP_TOKEN_STEP]
        # Without an owner and repositories the App would reach every repository it is
        # installed on, so the token is minted only when all four names are set.
        assert mint["if"] == APP_TOKEN_CONDITION, name
        # A step's condition cannot read a secret; the job says only whether the key is set.
        assert job["env"] == {
            "VECTEN_GIT_APP_KEY_SET": "${{ secrets.VECTEN_GIT_APP_PRIVATE_KEY != '' }}"
        }, name
        assert mint["uses"] == VERSIONS.github_actions["create_github_app_token"].split(" #")[0]
        assert mint["with"] == {
            "client-id": "${{ vars.VECTEN_GIT_APP_ID }}",
            "private-key": secret_ref("VECTEN_GIT_APP_PRIVATE_KEY"),
            "owner": "${{ vars.VECTEN_GIT_APP_OWNER }}",
            "repositories": "${{ vars.VECTEN_GIT_APP_REPOSITORIES }}",
            "permission-contents": "read",
        }, name
        shipping = [step.get("run") for step in steps(job)].index(command)
        assert ids.index(APP_TOKEN_STEP) < shipping, name


@pytest.mark.parametrize("params", DEPLOYED, ids=label)
def test_the_ship_workflow_passes_the_git_secrets_by_name(render, params):
    ship = render(params)["ship.yml"]["jobs"]["ship"]
    assert ship["secrets"] != "inherit"
    for name in GIT_SECRETS:
        assert ship["secrets"][name] == secret_ref(name)


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


# Promotion: the commit input, commit statuses and the CLI the promoted commit pins

SHIPPING = ("deploy-platform.yml", "terraform-apply.yml")


def _vecten_job(doc: dict) -> dict:
    [job] = [j for j in doc["jobs"].values() if any("vecten " in c for c in runs(j))]
    return job


@pytest.mark.parametrize("params", DEPLOYED, ids=label)
def test_the_shipping_workflows_take_a_commit_that_defaults_to_the_runs_own(render, params):
    docs = render(params)
    for name in SHIPPING:
        on = triggers(docs[name])
        dispatched = on["workflow_dispatch"]["inputs"]["commit"]
        assert dispatched["required"] is False and dispatched.get("default", "") == "", name
        job = _vecten_job(docs[name])
        [command] = [c for c in runs(job) if c.startswith(("vecten ship", "vecten deploy"))]
        assert '${COMMIT:+--commit "$COMMIT"}' in command, name
        [step] = [s for s in steps(job) if s.get("run") == command]
        assert step["env"]["COMMIT"] == "${{ inputs.commit }}", name
        # Full history, so the commit and the default branch's ancestry are in the checkout.
        [checkout] = [s for s in steps(job) if "actions/checkout" in s.get("uses", "")]
        assert checkout["with"]["fetch-depth"] == 0, name
    called = triggers(docs["deploy-platform.yml"])["workflow_call"]["inputs"]["commit"]
    assert called == {"type": "string", "required": False, "default": ""}


@pytest.mark.parametrize("params", DEPLOYED, ids=label)
def test_every_shipping_job_may_set_commit_statuses(render, params):
    docs = render(params)
    for name in SHIPPING:
        job = _vecten_job(docs[name])
        assert permissions(docs[name], job)["statuses"] == "write", name
        [command] = [s for s in steps(job) if s.get("run", "").startswith("vecten ")]
        assert command["env"]["GH_TOKEN"] == "${{ github.token }}", name
    # A called workflow's token holds at most what the calling job grants.
    caller = docs["ship.yml"]["jobs"]["ship"]
    assert permissions(docs["ship.yml"], caller)["statuses"] == "write"


@pytest.mark.parametrize("params", DEPLOYED, ids=label)
def test_the_shipping_jobs_install_the_cli_the_commit_pins(render, params, tmp_path):
    docs = render(params)
    for name in SHIPPING:
        job = _vecten_job(docs[name])
        [version] = [s for s in steps(job) if s.get("id") == "version"]
        assert version["env"] == {"COMMIT": "${{ inputs.commit }}"}
        [install] = [s for s in steps(job) if "uv tool install" in s.get("run", "")]
        assert install["run"] == 'uv tool install "vecten==$VECTEN_VERSION"'
        assert install["env"] == {"VECTEN_VERSION": "${{ steps.version.outputs.vecten }}"}
        order = [s.get("id") or s.get("run", s.get("uses", "")) for s in steps(job)]
        assert order.index("version") < order.index(install["run"]), name
        assert not any(f"vecten=={VERSIONS.vecten}" in c for c in runs(job)), name


def _run_version_step(script: str, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-e", "-c", script], cwd=cwd, env=env, capture_output=True, text=True
    )


@pytest.mark.parametrize("params", AWS[:1], ids=label)
def test_the_version_step_reads_scaffold_version_at_the_commit(render, params, tmp_path):
    [version] = [
        s
        for s in steps(_vecten_job(render(params)["deploy-platform.yml"]))
        if s.get("id") == "version"
    ]
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "T")
    git("config", "user.email", "t@acme.test")
    (repo / "vecten.toml").write_text('[project]\nname = "acme"\nscaffold_version = "0.1.1"\n')
    git("add", "-A")
    git("commit", "-q", "-m", "a")
    old = git("rev-parse", "HEAD")
    (repo / "vecten.toml").write_text('[project]\nname = "acme"\nscaffold_version = "0.2.0"\n')
    git("commit", "-q", "-am", "b")
    head = git("rev-parse", "HEAD")
    base = {"PATH": os.environ["PATH"], "GITHUB_SHA": head}
    for commit, expected in (("", "0.2.0"), (old, "0.1.1"), (old[:7], "0.1.1")):
        out = tmp_path / f"out-{expected}-{len(commit)}"
        env = {**base, "COMMIT": commit, "GITHUB_OUTPUT": str(out)}
        result = _run_version_step(version["run"], repo, env)
        assert result.returncode == 0, result.stderr
        assert out.read_text() == f"vecten={expected}\n"
    for bad in ("--output=/tmp/x", "HEAD~1"):
        env = {**base, "COMMIT": bad, "GITHUB_OUTPUT": str(tmp_path / "bad")}
        result = _run_version_step(version["run"], repo, env)
        assert result.returncode != 0 and "commit id" in result.stderr
        assert not (tmp_path / "bad").exists() or (tmp_path / "bad").read_text() == ""


def _version_repo(tmp_path: Path, versions: list[str | None]) -> tuple[Path, list[str]]:
    """A repository with one commit per entry: its vecten.toml names that scaffold_version,
    or holds no [project] table for None."""
    repo = tmp_path / "versions"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "T")
    git("config", "user.email", "t@acme.test")
    commits = []
    for version in versions:
        text = "users = 1\n" if version is None else f'[project]\nscaffold_version = "{version}"\n'
        (repo / "vecten.toml").write_text(text)
        git("add", "-A")
        git("commit", "-q", "--allow-empty", "-m", str(version))
        commits.append(git("rev-parse", "HEAD"))
    return repo, commits


@pytest.mark.parametrize("params", AWS[:1], ids=label)
def test_the_version_step_takes_post_and_dev_releases_and_names_a_missing_version(
    render, params, tmp_path
):
    [version] = [
        s
        for s in steps(_vecten_job(render(params)["terraform-apply.yml"]))
        if s.get("id") == "version"
    ]
    repo, commits = _version_repo(tmp_path, ["0.1.1.post2", "0.2.0.dev3", "0.2.0rc1", None])
    for commit, expected in zip(
        commits[:3], ["0.1.1.post2", "0.2.0.dev3", "0.2.0rc1"], strict=True
    ):
        out = tmp_path / f"out-{expected}"
        env = {
            "PATH": os.environ["PATH"],
            "GITHUB_SHA": commit,
            "COMMIT": "",
            "GITHUB_OUTPUT": str(out),
        }
        result = _run_version_step(version["run"], repo, env)
        assert result.returncode == 0, result.stderr
        assert out.read_text() == f"vecten={expected}\n"
    env = {
        "PATH": os.environ["PATH"],
        "GITHUB_SHA": commits[3],
        "COMMIT": "",
        "GITHUB_OUTPUT": str(tmp_path / "missing"),
    }
    result = _run_version_step(version["run"], repo, env)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "[project] scaffold_version" in result.stderr


@pytest.mark.parametrize("params", DEPLOYED, ids=label)
def test_no_run_line_interpolates_an_expression(render, params):
    # `${{ }}` lands in the shell script before bash reads it; values reach a step by env.
    for name, doc in render(params).items():
        if name == "terraform-plan.yml":
            continue
        for job_name, job in doc["jobs"].items():
            for step in steps(job):
                assert "${{" not in step.get("run", ""), (name, job_name, step["run"])


@pytest.mark.parametrize("params", DEPLOYED, ids=label)
def test_the_pull_request_plan_names_prod_on_purpose(raw, params):
    # A merge reaches prod last, so the plan a reviewer reads is prod's, made with prod's
    # read-only plan identity, whatever the promotion order.
    text = raw(params, "terraform-plan.yml")
    lines = [
        ln.strip().removeprefix("- ")
        for ln in text.splitlines()
        if re.search(r"\bprod\b", ln) and not ln.strip().startswith("#")
    ]
    assert lines, "terraform-plan.yml plans prod"
    for line in lines:
        assert re.search(r"--env prod\b|_PROD\b|plans/prod\.txt|plan for prod\b", line), line


@pytest.mark.parametrize("params", DEPLOYED, ids=label)
def test_no_shipping_workflow_names_prod_outside_its_defaults(render, raw, params):
    for name in ("ship.yml", *SHIPPING):
        text = raw(params, name)
        lines = [
            ln
            for ln in text.splitlines()
            if re.search(r"\bprod\b", ln) and not ln.strip().startswith("#")
        ]
        assert all(ln.strip() == "default: prod" for ln in lines), (name, lines)


@pytest.mark.parametrize("params", DEPLOYED, ids=label)
def test_every_needs_output_reference_names_a_declared_output(render, raw, params):
    for name, doc in render(params).items():
        declared = {
            job_name: set((job.get("outputs") or {}).keys())
            for job_name, job in doc["jobs"].items()
        }
        for job_name, output in re.findall(
            r"needs\.([\w-]+)\.outputs\.([\w-]+)", raw(params, name)
        ):
            assert output in declared.get(job_name, set()), (name, job_name, output)
