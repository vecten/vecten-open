import json

import boto3
import pytest
from moto import mock_aws

from vecten.deploy.aws import clients, plan_role_variable
from vecten.deploy.aws.bootstrap import GITHUB_OIDC_URL
from vecten.github import gh as gh_mod
from vecten.next.deploy import deploy_prerequisite_items
from vecten.next.gitstate import GitState
from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_shared.config import load_config

from .deploy_fixtures import (
    MANAGED_POLICIES,
    OWNER_ID,
    REGION,
    REPOSITORY_ID,
    STATE_BUCKET,
    FakeGh,
    aws_credentials,
    no_aws_credentials,
    render_aws,
    set_facts,
)
from .scaffold_matrix import LOCAL

ACCOUNT = "123456789012"
IMMUTABLE = f"repo:acme@{OWNER_ID}/data@{REPOSITORY_ID}"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/vecten-acme-prod-deploy"
PLAN_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/vecten-acme-prod-plan"
BOUNDARY = f"arn:aws:iam::{ACCOUNT}:policy/vecten-acme-prod-boundary"
VARIABLES = ["VECTEN_AWS_ROLE_ARN", "VECTEN_AWS_REGION", "VECTEN_STATE_BUCKET"]
CLEAN = GitState(has_repo=True, has_origin=True, dirty=False, ahead=False)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = render_aws(tmp_path / "proj")
    monkeypatch.chdir(root)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return root


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGh()
    monkeypatch.setattr(gh_mod, "get_gh", lambda: fake)
    return fake


@pytest.fixture
def aws(project, monkeypatch):
    aws_credentials(monkeypatch, project)
    with mock_aws(config=MANAGED_POLICIES):
        yield


@pytest.fixture
def no_cloud(project, monkeypatch):
    no_aws_credentials(monkeypatch, project)

    def refuse(service, region):
        raise AssertionError(f"a {service} client was created")

    monkeypatch.setattr(clients, "client", refuse)
    return project


def doc(result):
    return json.loads(result.stdout)


def _iam():
    return boto3.client("iam", region_name=REGION)


def _oidc_count():
    return len(_iam().list_open_id_connect_providers()["OpenIDConnectProviderList"])


def _role_count():
    return len(_iam().list_roles()["Roles"])


def test_bootstrap_needs_an_environment(invoke, project):
    r = invoke("bootstrap", "--json")
    assert r.exit_code == 2
    assert "--env" in doc(r)["error"]


def test_first_bootstrap_prepares_the_environment(invoke, project, aws, gh):
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    assert data["env"] == "prod"
    assert data["state_bucket"] == STATE_BUCKET
    assert data["role_arn"] == ROLE_ARN
    assert data["plan_role_arn"] == PLAN_ROLE_ARN
    assert data["repository"] == "acme/data"
    assert "repository_note" not in data
    assert data["created"] == [
        "state_bucket",
        "oidc_provider",
        "boundary_policy",
        "deploy_role",
        "plan_role",
    ]
    assert data["environment_variables"] == VARIABLES
    assert data["repository_variables"] == ["VECTEN_AWS_PLAN_ROLE_ARN_PROD"]

    s3 = boto3.client("s3", region_name=REGION)
    assert s3.get_bucket_versioning(Bucket=STATE_BUCKET)["Status"] == "Enabled"
    rules = s3.get_bucket_encryption(Bucket=STATE_BUCKET)["ServerSideEncryptionConfiguration"]
    algorithm = rules["Rules"][0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
    assert algorithm == "AES256"
    block = s3.get_public_access_block(Bucket=STATE_BUCKET)["PublicAccessBlockConfiguration"]
    assert all(block.values())

    assert _oidc_count() == 1
    document = _iam().get_role(RoleName="vecten-acme-prod-deploy")["Role"]
    trust = document["AssumeRolePolicyDocument"]
    trust = json.loads(trust) if isinstance(trust, str) else trust
    sub = trust["Statement"][0]["Condition"]["StringEquals"][f"{GITHUB_OIDC_URL}:sub"]
    assert sub == ["repo:acme/data:environment:prod", f"{IMMUTABLE}:environment:prod"]

    assert gh.named("repo", "view") == [
        ["repo", "view", "acme/data", "--json", "nameWithOwner,defaultBranchRef"]
    ]
    env_path = "repos/acme/data/environments/prod"
    assert [c[:4] for c in gh.named("api")] == [
        ["api", "repos/acme/data"],
        ["api", env_path],
        ["api", "-X", "PUT", env_path],
        ["api", "-X", "POST", f"{env_path}/deployment-branch-policies"],
    ]
    assert data["environment_branch_policy"] == "default-branch"
    assert "warnings" not in data
    sets = gh.named("variable", "set")
    assert [c[2] for c in sets] == [*VARIABLES, "VECTEN_AWS_PLAN_ROLE_ARN_PROD"]
    values = {c[2]: c[c.index("--body") + 1] for c in sets}
    assert values == {
        "VECTEN_AWS_ROLE_ARN": ROLE_ARN,
        "VECTEN_AWS_REGION": REGION,
        "VECTEN_STATE_BUCKET": STATE_BUCKET,
        "VECTEN_AWS_PLAN_ROLE_ARN_PROD": PLAN_ROLE_ARN,
    }
    for call in sets[:3]:
        assert call[3:5] == ["--env", "prod"]
        assert call[-2:] == ["--repo", "acme/data"]
    assert "--env" not in sets[3]
    assert sets[3][-2:] == ["--repo", "acme/data"]

    plan = _iam().get_role(RoleName="vecten-acme-prod-plan")["Role"]["AssumeRolePolicyDocument"]
    plan = json.loads(plan) if isinstance(plan, str) else plan
    subjects = plan["Statement"][0]["Condition"]["StringEquals"][f"{GITHUB_OIDC_URL}:sub"]
    assert subjects == [
        "repo:acme/data:pull_request",
        "repo:acme/data:ref:refs/heads/main",
        f"{IMMUTABLE}:pull_request",
        f"{IMMUTABLE}:ref:refs/heads/main",
    ]
    assert _iam().get_policy(PolicyArn=BOUNDARY)["Policy"]["DefaultVersionId"] == "v1"

    facts = load_config(project).environments["prod"]
    assert facts.bootstrapped is True
    assert facts.state_bucket == STATE_BUCKET
    assert 'state_bucket = "vecten-acme-prod-state-123456789012"' in (
        project / "vecten.toml"
    ).read_text(encoding="utf-8")


def test_bootstrap_is_safe_to_rerun(invoke, project, aws, gh):
    assert invoke("bootstrap", "--env", "prod", "--json").exit_code == 0
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    assert data["created"] == []
    assert data["role_arn"] == ROLE_ARN
    assert data["plan_role_arn"] == PLAN_ROLE_ARN
    assert _oidc_count() == 1
    assert _role_count() == 2
    assert len(_iam().list_policy_versions(PolicyArn=BOUNDARY)["Versions"]) == 1
    assert len(gh.named("variable", "set")) == 8
    assert data["environment_branch_policy"] == "unchanged"
    puts = [c for c in gh.named("api") if c[1:3] == ["-X", "PUT"]]
    assert len(puts) == 1


def test_an_oidc_provider_from_another_project_is_reused(invoke, project, aws, gh):
    existing = _iam().create_open_id_connect_provider(
        Url=f"https://{GITHUB_OIDC_URL}",
        ClientIDList=["sts.amazonaws.com"],
        ThumbprintList=["6938fd4d98bab03faadb97b34396831e3780aea1"],
    )["OpenIDConnectProviderArn"]
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["created"] == ["state_bucket", "boundary_policy", "deploy_role", "plan_role"]
    assert _oidc_count() == 1
    trust = _iam().get_role(RoleName="vecten-acme-prod-deploy")["Role"]["AssumeRolePolicyDocument"]
    trust = json.loads(trust) if isinstance(trust, str) else trust
    assert trust["Statement"][0]["Principal"] == {"Federated": existing}


def test_without_branch_policies_the_environment_stays_and_a_warning_names_the_runbook(
    invoke, project, aws, monkeypatch
):
    gh = FakeGh(branch_policies=False)
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    assert data["environment_branch_policy"] == "unavailable"
    (warning,) = data["warnings"]
    assert "docs/runbook.md" in warning
    assert "repos/acme/data/environments/prod" in gh.environments
    assert load_config(project).environments["prod"].bootstrapped is True


def test_an_existing_environment_open_to_every_branch_is_warned_about_and_left_alone(
    invoke, project, aws, monkeypatch
):
    path = "repos/acme/data/environments/prod"
    gh = FakeGh(environment_policies={path: (None, [])})
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    assert data["environment_branch_policy"] == "unrestricted"
    (warning,) = data["warnings"]
    assert "accepts deployments from any branch" in warning
    assert 'docs/runbook.md ("Protect the deploy environment")' in warning
    assert [c for c in gh.named("api") if "-X" in c] == []
    assert gh.environments[path] == (None, [])


def test_the_human_message_warns_when_branch_policies_are_unavailable(
    invoke, project, aws, monkeypatch
):
    monkeypatch.setattr(gh_mod, "get_gh", lambda: FakeGh(branch_policies=False))
    r = invoke("bootstrap", "--env", "prod")
    assert r.exit_code == 0, r.stdout
    assert "warning:" in r.stdout
    assert "docs/runbook.md" in r.stdout


def test_account_id_maps_a_missing_profile_to_a_prerequisite(project, monkeypatch):
    from botocore.exceptions import ProfileNotFound

    from vecten.deploy.control import get_control_plane
    from vecten_shared.output import ExitCode, Failure

    def missing(service, region):
        raise ProfileNotFound(profile="acme-admin")

    monkeypatch.setattr(clients, "client", missing)
    with pytest.raises(Failure) as caught:
        get_control_plane("aws", project, "prod").account_id()
    assert caught.value.code == ExitCode.PREREQUISITE
    assert "aws sso login" in caught.value.message


def test_plan_role_variable_is_named_per_environment():
    assert plan_role_variable("prod") == "VECTEN_AWS_PLAN_ROLE_ARN_PROD"
    assert plan_role_variable("prod-eu") == "VECTEN_AWS_PLAN_ROLE_ARN_PROD_EU"


def test_a_second_environment_keeps_the_first_ones_plan_role(invoke, project, aws, gh):
    prod = project / "deploy" / "envs" / "prod.tfvars"
    staging = prod.with_name("staging.tfvars")
    staging.write_text(
        prod.read_text(encoding="utf-8").replace('"prod"', '"staging"'), encoding="utf-8"
    )
    assert invoke("bootstrap", "--env", "prod", "--json").exit_code == 0
    r = invoke("bootstrap", "--env", "staging", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["repository_variables"] == ["VECTEN_AWS_PLAN_ROLE_ARN_STAGING"]
    repo_level = {
        c[2]: c[c.index("--body") + 1] for c in gh.named("variable", "set") if "--env" not in c
    }
    assert repo_level == {
        "VECTEN_AWS_PLAN_ROLE_ARN_PROD": PLAN_ROLE_ARN,
        "VECTEN_AWS_PLAN_ROLE_ARN_STAGING": f"arn:aws:iam::{ACCOUNT}:role/vecten-acme-staging-plan",
    }


def test_githubs_casing_of_the_repository_is_used_and_reported(invoke, project, aws, monkeypatch):
    gh = FakeGh(canonical="Acme/Data")
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    assert data["repository"] == "Acme/Data"
    assert "Acme/Data" in data["repository_note"]
    assert "acme/data" in data["repository_note"]
    trust = _iam().get_role(RoleName="vecten-acme-prod-deploy")["Role"]["AssumeRolePolicyDocument"]
    trust = json.loads(trust) if isinstance(trust, str) else trust
    sub = trust["Statement"][0]["Condition"]["StringEquals"][f"{GITHUB_OIDC_URL}:sub"]
    assert sub == [
        "repo:Acme/Data:environment:prod",
        f"repo:Acme@{OWNER_ID}/Data@{REPOSITORY_ID}:environment:prod",
    ]
    assert gh.named("api")[:2] == [
        ["api", "repos/Acme/Data"],
        ["api", "repos/Acme/Data/environments/prod"],
    ]


def test_another_repository_on_github_is_refused_before_aws_writes(
    invoke, project, aws, monkeypatch
):
    monkeypatch.setattr(gh_mod, "get_gh", lambda: FakeGh(canonical="acme/data-renamed"))
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 6, r.stdout
    assert "acme/data-renamed" in doc(r)["error"]
    assert boto3.client("s3", region_name=REGION).list_buckets()["Buckets"] == []
    assert _role_count() == 0


def test_without_aws_credentials_names_the_logins(invoke, project, gh, monkeypatch):
    no_aws_credentials(monkeypatch, project)
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 3, r.stdout
    assert "aws sso login" in doc(r)["error"]
    assert gh.named("api") == []
    assert load_config(project).environments["prod"].bootstrapped is False


def test_gh_logged_out_creates_nothing_in_aws(invoke, project, aws, monkeypatch):
    monkeypatch.setattr(gh_mod, "get_gh", lambda: FakeGh(logged_in=False))
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 3, r.stdout
    assert "gh auth login" in doc(r)["error"]
    assert boto3.client("s3", region_name=REGION).list_buckets()["Buckets"] == []
    assert _oidc_count() == 0
    assert _role_count() == 0
    assert load_config(project).environments["prod"].bootstrapped is False


def test_a_failed_variable_set_records_no_fact(invoke, project, aws, monkeypatch):
    monkeypatch.setattr(gh_mod, "get_gh", lambda: FakeGh(failing=("variable",)))
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 5, r.stdout
    assert load_config(project).environments["prod"].bootstrapped is False


def test_unknown_environment_file_is_refused(invoke, no_cloud, gh):
    r = invoke("bootstrap", "--env", "nosuch", "--json")
    assert r.exit_code == 4
    assert "deploy/envs/nosuch.tfvars" in doc(r)["error"]


def test_local_environment_is_refused(invoke, no_cloud, gh):
    r = invoke("bootstrap", "--env", "local", "--json")
    assert r.exit_code != 0
    assert gh.calls == []


def test_project_without_a_deploy_layer_names_init_deploy(invoke, tmp_path, monkeypatch, gh):
    root = tmp_path / "local"
    render_project(root, ScaffoldParams.from_answers({**LOCAL, "sources": ["hubspot"]}, {}))
    monkeypatch.chdir(root)
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 4
    assert "vecten init --deploy" in doc(r)["error"]


def test_scaleway_bootstrap_without_a_scaleway_login_names_scw_init(
    invoke, tmp_path, monkeypatch, gh
):
    root = render_aws(tmp_path / "scw", cloud="scaleway")
    monkeypatch.chdir(root)
    for name in ("SCW_ACCESS_KEY", "SCW_SECRET_KEY", "SCW_DEFAULT_ORGANIZATION_ID"):
        monkeypatch.delenv(name, raising=False)
    # On the laptop; inside GitHub Actions the message names the workflow's secrets instead.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("SCW_CONFIG_PATH", str(tmp_path / "no-scw-config.yaml"))
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 3
    assert "scw init" in doc(r)["error"]
    assert gh.calls == []
    assert load_config(root).environments["prod"].bootstrapped is False


def test_tfvars_repository_must_match_vecten_toml(invoke, no_cloud, gh):
    path = no_cloud / "deploy" / "envs" / "prod.tfvars"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('"acme/data"', '"acme/other"'), encoding="utf-8")
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 6
    error = doc(r)["error"]
    assert "acme/other" in error
    assert "acme/data" in error


def test_bootstrap_keeps_the_other_facts(invoke, project, aws, gh):
    set_facts(project, first_user_added=True)
    assert invoke("bootstrap", "--env", "prod", "--json").exit_code == 0
    facts = load_config(project).environments["prod"]
    assert facts.first_user_added is True
    assert facts.bootstrapped is True


def test_next_stops_offering_bootstrap_after_a_run(invoke, project, aws, gh):
    bootstrap = "vecten bootstrap --env prod"
    before = deploy_prerequisite_items(project, load_config(project), "prod", CLEAN)
    assert bootstrap in [item.resolution for item in before]
    assert invoke("bootstrap", "--env", "prod", "--json").exit_code == 0
    after = deploy_prerequisite_items(project, load_config(project), "prod", CLEAN)
    assert bootstrap not in [item.resolution for item in after]


def test_the_scaleway_message_says_a_deploy_hands_the_vms_their_new_keys():
    from vecten.cli.commands.deploy import _bootstrap_message

    data = {
        "env": "prod",
        "state_bucket": "acme-prod-state",
        "deploy_application": "app-deploy",
        "plan_application": "app-plan",
        "host_applications": ["platform", "mcp"],
        "created": [],
        "environment_variables": ["VECTEN_SCW_PROJECT_ID"],
        "repository_variables": [],
        "awaiting_ack": ["platform", "mcp"],
        "environment_branch_policy": "default-branch",
    }
    message = " ".join(_bootstrap_message(data).split())
    assert "the old keys of platform, mcp stay until each host acknowledges its new key" in message
    assert "run `vecten deploy --env prod` to hand the hosts their new keys" in message
