"""`vecten bootstrap` on Scaleway: projects, the state bucket, scoped applications and keys."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import timedelta

import pytest

from vecten.deploy.scaleway import bootstrap as bootstrap_mod
from vecten.deploy.scaleway import clients
from vecten.deploy.scaleway.bootstrap import Layout, deploy_rules, host_rules, plan_rules
from vecten.github import gh as gh_mod
from vecten_shared.config import load_config, save_config
from vecten_shared.output import ExitCode, Failure

from .deploy_fixtures import FakeGh, render_aws
from .scaleway_fakes import (
    DNS_PROJECT,
    OPERATOR_ACCESS,
    OPERATOR_SECRET,
    OPERATOR_USER,
    ORGANIZATION,
    expiry_of,
    install,
)

RESOURCES = "11111111-0000-4000-8000-000000000001"
STATE = "22222222-0000-4000-8000-000000000002"
DNS = "33333333-0000-4000-8000-000000000003"
ROOT = "/vecten/acme/prod"
ENV_VARIABLES = [
    "VECTEN_SCW_PROJECT_ID",
    "VECTEN_SCW_STATE_PROJECT_ID",
    "VECTEN_SCW_REGION",
    "VECTEN_STATE_BUCKET",
]
ENV_SECRETS = ["VECTEN_SCW_ACCESS_KEY", "VECTEN_SCW_SECRET_KEY"]
REPO_SECRETS = ["VECTEN_SCW_PLAN_ACCESS_KEY_PROD", "VECTEN_SCW_PLAN_SECRET_KEY_PROD"]
FIRST_CREATED = [
    "project",
    "state_project",
    "deploy_application",
    "deploy_policy",
    "plan_application",
    "plan_policy",
    "platform_application",
    "platform_policy",
    "mcp_application",
    "mcp_policy",
    "state_bucket",
    "deploy_key",
    "plan_key",
    "platform_key",
    "mcp_key",
]


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = render_aws(tmp_path / "proj", cloud="scaleway")
    monkeypatch.chdir(root)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return root


@pytest.fixture
def scw(project, monkeypatch, tmp_path):
    return install(monkeypatch, tmp_path)


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGh()
    monkeypatch.setattr(gh_mod, "get_gh", lambda: fake)
    return fake


def doc(result):
    return json.loads(result.stdout)


def _facts(project):
    return load_config(project).environments["prod"]


def _secret_sets(gh):
    return gh.named("secret", "set")


def _set_components(project, components: list[str]) -> None:
    config = load_config(project)
    config.components = components
    save_config(project, config)
    tfvars = project / "deploy" / "envs" / "prod.tfvars"
    text = re.sub(
        r"^components\s*=.*$",
        f"components        = {json.dumps(components)}",
        tfvars.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    tfvars.write_text(text, encoding="utf-8")


def _host_key(org, host: str) -> tuple[str, str]:
    value = org.secret_value(f"{ROOT}/deploy", f"HOST_KEY_{host.upper()}")
    assert value is not None
    access, secret = value.split(":", 1)
    return access, secret


# --- the policy documents ----------------------------------------------------------------


STANDARD = Layout(components=("platform", "mcp"), profile="standard", warehouse="postgres")


def _by_sets(rules: list[dict]) -> dict[tuple[str, ...], dict]:
    return {tuple(r["permission_set_names"]): r for r in rules}


def test_the_platform_host_policy_reads_its_scope_and_creates_only_airflow_keys():
    rules = host_rules(STANDARD, "acme", "prod", RESOURCES, "platform")
    platform = f'resource.name.startsWith("{ROOT}/platform/")'
    airflow = " || ".join(
        f'resource.name == "{ROOT}/platform/{n}"'
        for n in (
            "AIRFLOW_FERNET_KEY",
            "AIRFLOW_API_SECRET_KEY",
            "AIRFLOW_JWT_SECRET",
            "AIRFLOW_ADMIN_PASSWORD",
            "AIRFLOW_BASIC_AUTH_PASSWORD",
        )
    )
    assert rules == [
        {
            "permission_set_names": ["SecretManagerSecretAccess"],
            "project_ids": [RESOURCES],
            "condition": platform,
        },
        {
            "permission_set_names": ["SecretManagerReadOnly"],
            "project_ids": [RESOURCES],
            "condition": f"{platform} || !has(resource.id)",
        },
        {
            "permission_set_names": ["SecretManagerSecretCreate"],
            "project_ids": [RESOURCES],
            "condition": airflow,
        },
        {
            "permission_set_names": ["ObjectStorageObjectsRead", "ObjectStorageObjectsWrite"],
            "project_ids": [RESOURCES],
        },
    ]


def test_the_setup_host_policy_adds_setup_and_the_exact_generated_names():
    rules = _by_sets(host_rules(STANDARD, "acme", "prod", RESOURCES, "mcp"))
    access = (
        f'resource.name.startsWith("{ROOT}/mcp/") || '
        f'resource.name.startsWith("{ROOT}/setup/") || '
        f'resource.name == "{ROOT}/platform/AIRFLOW_DB_PASSWORD" || '
        f'resource.name == "{ROOT}/platform/LOADER_PASSWORD"'
    )
    assert rules[("SecretManagerSecretAccess",)]["condition"] == access
    assert rules[("SecretManagerReadOnly",)]["condition"] == f"{access} || !has(resource.id)"
    assert rules[("SecretManagerSecretCreate",)]["condition"] == (
        f'resource.name == "{ROOT}/platform/AIRFLOW_DB_PASSWORD" || '
        f'resource.name == "{ROOT}/platform/LOADER_PASSWORD" || '
        f'resource.name == "{ROOT}/mcp/MCP_STATE_DB_PASSWORD" || '
        f'resource.name == "{ROOT}/mcp/MCP_PROVISIONER_PASSWORD" || '
        f'resource.name == "{ROOT}/mcp/MCP_IMPERSONATOR_PASSWORD" || '
        f'resource.name == "{ROOT}/mcp/MCP_SIGNING_KEY"'
    )
    assert set(rules) == {
        ("SecretManagerSecretAccess",),
        ("SecretManagerReadOnly",),
        ("SecretManagerSecretCreate",),
        ("ObjectStorageObjectsRead", "ObjectStorageObjectsWrite"),
    }


def test_the_minimal_host_reads_both_scopes_and_setup():
    minimal = Layout(components=("platform", "mcp"), profile="minimal", warehouse="postgres")
    assert minimal.hosts == {"main": ["platform", "mcp"]}
    assert minimal.setup_host == "main"
    rules = _by_sets(host_rules(minimal, "acme", "prod", RESOURCES, "main"))
    assert rules[("SecretManagerSecretAccess",)]["condition"] == (
        f'resource.name.startsWith("{ROOT}/platform/") || '
        f'resource.name.startsWith("{ROOT}/mcp/") || '
        f'resource.name.startsWith("{ROOT}/setup/")'
    )
    create = rules[("SecretManagerSecretCreate",)]["condition"]
    assert f'"{ROOT}/platform/AIRFLOW_FERNET_KEY"' in create
    assert f'"{ROOT}/mcp/MCP_IMPERSONATOR_PASSWORD"' in create
    assert "startsWith" not in create


@pytest.mark.parametrize(
    "layout",
    [
        STANDARD,
        Layout(components=("platform", "mcp"), profile="minimal", warehouse="postgres"),
        Layout(components=("mcp",), profile="standard", warehouse="postgres"),
        Layout(components=("platform",), profile="standard", warehouse="snowflake"),
    ],
)
def test_no_host_rule_reaches_the_state_project_or_reads_a_secret_unconditioned(layout):
    for host in layout.hosts:
        rules = host_rules(layout, "acme", "prod", RESOURCES, host)
        for rule in rules:
            assert rule["project_ids"] == [RESOURCES]
            assert STATE not in json.dumps(rule)
            assert "organization_id" not in rule
            sets = rule["permission_set_names"]
            if any(s.startswith("SecretManager") for s in sets):
                assert rule.get("condition"), (host, rule)
            if "SecretManagerSecretAccess" in sets or "SecretManagerSecretCreate" in sets:
                assert "!has" not in rule["condition"]
            terms = rule["condition"].split(" || ") if "condition" in rule else []
            for term in terms:
                if term.startswith("resource.name.startsWith"):
                    assert re.fullmatch(
                        r'resource\.name\.startsWith\("/vecten/acme/prod/\w+/"\)', term
                    )
                elif term != "!has(resource.id)":
                    assert re.fullmatch(r'resource\.name == "/vecten/acme/prod/\w+/[A-Z_]+"', term)
            assert not any(s.startswith("IAM") for s in sets)
            assert "SecretManagerFullAccess" not in sets


def test_a_host_never_reads_the_deploy_scope_where_vm_keys_live():
    for layout in (STANDARD, Layout(("platform", "mcp"), "minimal", "postgres")):
        for host in layout.hosts:
            text = json.dumps(host_rules(layout, "acme", "prod", RESOURCES, host))
            assert "/deploy/" not in text
            assert "/admin/" not in text


def test_the_deploy_and_plan_applications_hold_no_iam_permission_set():
    for rules in (
        deploy_rules(RESOURCES, STATE, DNS),
        plan_rules(STATE),
        deploy_rules(RESOURCES, STATE, RESOURCES),
    ):
        for rule in rules:
            assert not any(s.startswith("IAM") for s in rule["permission_set_names"]), rule
            assert "organization_id" not in rule


def test_the_deploy_rules_split_resources_dns_and_state():
    assert deploy_rules(RESOURCES, STATE, DNS) == [
        {
            "permission_set_names": [
                "InstancesFullAccess",
                "PrivateNetworksFullAccess",
                "IPAMReadOnly",
                "RelationalDatabasesFullAccess",
                "SecretManagerFullAccess",
                "ObjectStorageFullAccess",
                "ObjectStorageBucketPolicyFullAccess",
                "BlockStorageFullAccess",
            ],
            "project_ids": [RESOURCES],
        },
        {"permission_set_names": ["DomainsDNSFullAccess"], "project_ids": [DNS]},
        {
            "permission_set_names": [
                "ObjectStorageObjectsRead",
                "ObjectStorageObjectsWrite",
                "ObjectStorageObjectsDelete",
                "ObjectStorageBucketsRead",
            ],
            "project_ids": [STATE],
        },
    ]
    same = deploy_rules(RESOURCES, STATE, RESOURCES)
    assert "DomainsDNSFullAccess" in same[0]["permission_set_names"]
    assert len(same) == 2


def test_the_deploy_application_manages_block_storage_in_the_resources_project_only():
    # POP2 and PRO2 servers boot from an SBS volume, which the provider manages through the
    # Block Storage API.
    rules = deploy_rules(RESOURCES, STATE, DNS)
    holding = [
        r["project_ids"] for r in rules if "BlockStorageFullAccess" in r["permission_set_names"]
    ]
    assert holding == [[RESOURCES]]


def test_the_plan_application_holds_no_write_capable_set_beyond_state_locks():
    for rule in plan_rules(STATE):
        writes = [
            s
            for s in rule["permission_set_names"]
            if any(w in s for w in ("FullAccess", "Write", "Create", "Delete"))
        ]
        # Lock files need these; the state bucket policy limits them to <env>/*.tflock.
        assert rule["project_ids"] == [STATE]
        assert writes == ["ObjectStorageObjectsWrite", "ObjectStorageObjectsDelete"]


def test_the_plan_application_reads_nothing_in_the_resources_project():
    # The root module declares no data source and pull-request plans skip the refresh, so
    # the plan key needs no resources-project set; above all none reading instance user data.
    assert plan_rules(STATE) == [
        {
            "permission_set_names": [
                "ObjectStorageObjectsRead",
                "ObjectStorageObjectsWrite",
                "ObjectStorageObjectsDelete",
            ],
            "project_ids": [STATE],
        }
    ]
    every = [s for r in plan_rules(STATE) for s in r["permission_set_names"]]
    assert not any(
        s.startswith(("Instances", "RelationalDatabases", "SecretManager")) for s in every
    )


def test_the_state_bucket_policy_confines_the_plan_key_to_lock_files():
    policy = bootstrap_mod.state_bucket_policy(
        "b", "prod", deploy_app="D", plan_app="P", operator="user_id:U"
    )
    assert policy["Version"] == "2023-04-17"
    by_sid = {s["Sid"]: s for s in policy["Statement"]}
    assert by_sid["PlanWritesLocks"]["Resource"] == ["b/prod/*.tflock"]
    assert by_sid["PlanWritesLocks"]["Principal"] == {"SCW": "application_id:P"}
    assert set(by_sid["PlanReadsState"]["Action"]) == {"s3:GetObject", "s3:ListBucket"}
    assert by_sid["DeployObjects"]["Principal"] == {"SCW": "application_id:D"}
    assert by_sid["OperatorFullAccess"]["Principal"] == {"SCW": "user_id:U"}
    plan_actions = {
        a for s in policy["Statement"] if "P" in json.dumps(s["Principal"]) for a in s["Action"]
    }
    assert plan_actions == {"s3:GetObject", "s3:ListBucket", "s3:PutObject", "s3:DeleteObject"}


# --- bootstrap end to end ----------------------------------------------------------------


def test_first_bootstrap_creates_projects_bucket_applications_and_keys(invoke, project, scw, gh):
    org, storage, clock = scw
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    assert data["created"] == FIRST_CREATED
    facts = _facts(project)
    ids = facts.cloud_ids
    assert set(ids) == {
        "project",
        "state_project",
        "dns_project",
        "operator_user",
        "app_deploy",
        "app_plan",
        "app_platform",
        "app_mcp",
        "key_deploy",
        "key_plan",
        "expires_deploy",
        "expires_plan",
        "expires_platform",
        "expires_mcp",
    }
    year_on = (clock() + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert {ids[f"expires_{o}"] for o in ("deploy", "plan", "platform", "mcp")} == {year_on}
    resources, state = ids["project"], ids["state_project"]
    assert org.projects[resources]["name"] == "vecten-acme-prod"
    assert org.projects[state]["name"] == "vecten-acme-prod-state"
    assert facts.bootstrapped is True
    bucket = f"vecten-acme-prod-state-{state[:8]}"
    assert facts.state_bucket == bucket == data["state_bucket"]
    assert data["project_id"] == resources and data["state_project_id"] == state

    # The state bucket lives in the state project: boto3 signs with `<access>@<state>`.
    (create,) = storage.named("create_bucket")
    assert create[1].endswith(f"@{state}")
    assert create[2]["CreateBucketConfiguration"] == {"LocationConstraint": "fr-par"}
    stored = storage.buckets[bucket]
    assert stored["versioning"] == {"Status": "Enabled"}
    rule = stored["lifecycle"]["Rules"][0]
    assert rule["Status"] == "Enabled" and "NoncurrentVersionExpiration" in rule
    policy = json.loads(stored["policy"])
    principals = {s["Sid"]: s["Principal"]["SCW"] for s in policy["Statement"]}
    assert principals["DeployObjects"] == f"application_id:{ids['app_deploy']}"
    assert principals["PlanWritesLocks"] == f"application_id:{ids['app_plan']}"
    assert principals["OperatorFullAccess"] == f"user_id:{OPERATOR_USER}"
    # The policy is in place before any other bucket setting, so the operator keeps access.
    order = [c[0] for c in storage.calls if c[0].startswith("put_")]
    assert order[0] == "put_bucket_policy"

    # One application per identity, each with one policy and one key that expires.
    for name in ("deploy", "plan", "platform", "mcp"):
        app = org.app_named(f"vecten-acme-prod-{name}")
        assert app is not None and app["id"] == ids[f"app_{name}"]
        assert app["organization_id"] == ORGANIZATION
        (key,) = org.keys_of(app["id"])
        assert expiry_of(org, key["access_key"]) == clock() + timedelta(days=365)
        assert key["default_project_id"] == resources
    stored_rules = [
        {k: v for k, v in rule.items() if k != "id"}
        for rule in org.rules_of_app("vecten-acme-prod-deploy")
    ]
    assert stored_rules == deploy_rules(resources, state, DNS_PROJECT)

    # VM keys go to the deploy scope of the resources project's Secret Manager.
    for host in ("platform", "mcp"):
        access, secret = _host_key(org, host)
        (key,) = org.keys_of(ids[f"app_{host}"])
        assert key["access_key"] == access
        assert secret in org.issued_secrets
    stored_secret = next(s for s in org.secrets.values() if s["name"] == "HOST_KEY_MCP")
    assert stored_secret["project_id"] == resources
    assert stored_secret["path"] == f"{ROOT}/deploy"
    assert stored_secret["region"] == "fr-par"

    # GitHub: the environment, its variables and the deploy key; the plan key at repo level.
    variables = gh.named("variable", "set")
    assert [c[2] for c in variables] == ENV_VARIABLES
    values = {c[2]: c[c.index("--body") + 1] for c in variables}
    assert values == {
        "VECTEN_SCW_PROJECT_ID": resources,
        "VECTEN_SCW_STATE_PROJECT_ID": state,
        "VECTEN_SCW_REGION": "fr-par",
        "VECTEN_STATE_BUCKET": bucket,
    }
    sets = _secret_sets(gh)
    assert sets == [
        ["secret", "set", "VECTEN_SCW_ACCESS_KEY", "--env", "prod", "--repo", "acme/data"],
        ["secret", "set", "VECTEN_SCW_SECRET_KEY", "--env", "prod", "--repo", "acme/data"],
        ["secret", "set", "VECTEN_SCW_PLAN_ACCESS_KEY_PROD", "--repo", "acme/data"],
        ["secret", "set", "VECTEN_SCW_PLAN_SECRET_KEY_PROD", "--repo", "acme/data"],
    ]
    piped = {argv[2]: value for argv, value in gh.inputs}
    (deploy_key,) = org.keys_of(ids["app_deploy"])
    (plan_key,) = org.keys_of(ids["app_plan"])
    assert piped["VECTEN_SCW_ACCESS_KEY"] == deploy_key["access_key"]
    assert piped["VECTEN_SCW_PLAN_ACCESS_KEY_PROD"] == plan_key["access_key"]
    assert {piped["VECTEN_SCW_SECRET_KEY"], piped["VECTEN_SCW_PLAN_SECRET_KEY_PROD"]} <= set(
        org.issued_secrets
    )
    assert data["environment_variables"] == ENV_VARIABLES
    assert data["environment_secrets"] == ENV_SECRETS
    assert data["repository_secrets"] == REPO_SECRETS
    assert data["repository_variables"] == []
    env_path = "repos/acme/data/environments/prod"
    assert [c[:4] for c in gh.named("api")] == [
        ["api", "repos/acme/data"],
        ["api", env_path],
        ["api", "-X", "PUT", env_path],
        ["api", "-X", "POST", f"{env_path}/deployment-branch-policies"],
    ]
    assert data["environment_branch_policy"] == "default-branch"


def test_no_secret_value_reaches_an_argv_the_json_or_vecten_toml(invoke, project, scw, gh):
    org, storage, clock = scw
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    clock.advance(days=340)
    r2 = invoke("bootstrap", "--env", "prod", "--json")
    assert r2.exit_code == 0, r2.stdout
    secrets = [*org.issued_secrets, OPERATOR_SECRET]
    assert len(org.issued_secrets) == 8
    toml = (project / "vecten.toml").read_text(encoding="utf-8")
    argv = json.dumps(gh.calls)
    for secret in secrets:
        assert secret not in r.stdout and secret not in r2.stdout
        assert secret not in argv
        assert secret not in toml
        assert secret not in json.dumps(storage.calls)
        assert secret not in json.dumps(org.calls)
    # Secret values reach gh only on stdin, and Secret Manager only base64 in a version body.
    assert all(argv_[:2] == ["secret", "set"] for argv_, _ in gh.inputs)


def test_bootstrap_is_safe_to_rerun(invoke, project, scw, gh):
    org, storage, _ = scw
    assert invoke("bootstrap", "--env", "prod", "--json").exit_code == 0
    facts_before = dict(_facts(project).cloud_ids)
    keys_before = set(org.keys)
    writes_before = len(org.writes())
    secret_sets_before = len(_secret_sets(gh))
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    assert data["created"] == []
    assert data["rotated"] == [] and data["deleted"] == [] and data["updated"] == []
    assert data["environment_secrets"] == [] and data["repository_secrets"] == []
    assert set(org.keys) == keys_before
    assert _facts(project).cloud_ids == facts_before
    assert len(_secret_sets(gh)) == secret_sets_before
    # A rerun only reads the REST APIs; the bucket settings are reapplied through S3.
    assert org.writes()[writes_before:] == []
    assert len(storage.named("create_bucket")) == 1


def test_a_changed_rule_set_is_rewritten_in_place(invoke, project, scw, gh):
    org, _, _ = scw
    assert invoke("bootstrap", "--env", "prod", "--json").exit_code == 0
    policy_id = org.policy_of(_facts(project).cloud_ids["app_mcp"])["id"]
    org.rules[policy_id] = [
        {"id": "x", "permission_set_names": ["SecretManagerFullAccess"], "project_ids": [RESOURCES]}
    ]
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["updated"] == ["mcp_policy"]
    sets = [r_["permission_set_names"] for r_ in org.rules[policy_id]]
    assert ["SecretManagerFullAccess"] not in sets


def test_bootstrap_reconciles_hosts_and_rotates_near_expiry(invoke, project, scw, gh):
    org, storage, clock = scw
    assert invoke("bootstrap", "--env", "prod", "--json").exit_code == 0
    first = dict(_facts(project).cloud_ids)
    old_platform, _ = _host_key(org, "platform")
    old_mcp, _ = _host_key(org, "mcp")

    # mcp goes away: its application, policy, keys and stored key are deleted.
    _set_components(project, ["platform"])
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    assert data["created"] == []
    assert data["deleted"] == ["mcp_key", "mcp_policy", "mcp_application", "HOST_KEY_MCP"]
    assert org.app_named("vecten-acme-prod-mcp") is None
    assert old_mcp not in org.keys
    assert org.secret_value(f"{ROOT}/deploy", "HOST_KEY_MCP") is None
    ids = _facts(project).cloud_ids
    assert "app_mcp" not in ids and ids["app_platform"] == first["app_platform"]
    # The platform host is now the setup host, so its rules grow to the setup scope.
    assert data["updated"] == ["platform_policy"]
    assert old_platform in org.keys

    # mcp comes back: a new application and key; platform's key is kept.
    _set_components(project, ["platform", "mcp"])
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    assert data["created"] == ["mcp_application", "mcp_policy", "mcp_key"]
    assert data["updated"] == ["platform_policy"]
    assert _host_key(org, "platform")[0] == old_platform
    assert _facts(project).cloud_ids["app_mcp"] != first["app_mcp"]

    # 340 days on, every key expires within 30 days: hosts get a new key as a new version,
    # the old one kept until the host acknowledges the new one; the CI keys are replaced.
    clock.advance(days=340)
    old_deploy = _facts(project).cloud_ids["key_deploy"]
    mcp_before, _ = _host_key(org, "mcp")
    versions = org.secret_versions(f"{ROOT}/deploy", "HOST_KEY_PLATFORM")
    sets_before = len(_secret_sets(gh))
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    assert data["rotated"] == ["deploy_key", "plan_key", "platform_key", "mcp_key"]
    new_platform, _ = _host_key(org, "platform")
    assert new_platform != old_platform
    assert org.secret_versions(f"{ROOT}/deploy", "HOST_KEY_PLATFORM") == versions + 1
    assert old_platform in org.keys and mcp_before in org.keys
    ids = _facts(project).cloud_ids
    assert ids["retiring_platform"] == old_platform
    assert ids["retiring_mcp"] == mcp_before
    assert data["awaiting_ack"] == ["platform", "mcp"]
    # GitHub holds the new CI keys; the old ones stay for a run already under way and go on
    # the next bootstrap.
    assert old_deploy in org.keys
    assert ids["retiring_deploy"] == old_deploy
    assert "retiring_plan" in ids
    assert ids["key_deploy"] != old_deploy and ids["key_deploy"] in org.keys
    assert data["deleted"] == []
    assert data["ci_keys_retiring"] == ["deploy", "plan"]
    assert len(_secret_sets(gh)) == sets_before + 4
    assert expiry_of(org, new_platform) == clock() + timedelta(days=365)
    year_on = (clock() + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert ids["expires_deploy"] == ids["expires_platform"] == year_on

    # Without the acknowledgement the old host key stays; the old CI keys go.
    old_plan = ids["retiring_plan"]
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert doc(r)["rotated"] == [] and old_platform in org.keys
    assert doc(r)["deleted"] == ["deploy_retired_key", "plan_retired_key"]
    assert old_deploy not in org.keys and old_plan not in org.keys
    assert doc(r)["environment_secrets"] == [] and doc(r)["repository_secrets"] == []
    ids = _facts(project).cloud_ids
    assert "retiring_deploy" not in ids and "retiring_plan" not in ids
    assert ids["retiring_platform"] == old_platform

    # The platform host acknowledges its new key; the next run deletes the old one, checking
    # the acknowledgement with the new key itself.
    bucket = f"vecten-acme-prod-artifacts-{ids['project'][:8]}"
    digest = hashlib.sha256(new_platform.encode()).hexdigest()
    storage.buckets[bucket] = {"objects": {f"platform/key-ack/{digest}.json": b"{}"}}
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    assert data["deleted"] == ["platform_retired_key"]
    assert old_platform not in org.keys and mcp_before in org.keys
    ids = _facts(project).cloud_ids
    assert "retiring_platform" not in ids and ids["retiring_mcp"] == mcp_before
    head = [c for c in storage.named("head_object") if c[2]["Key"].endswith(f"{digest}.json")]
    assert head and all(c[1].startswith(f"{new_platform}@") for c in head)


def test_a_key_left_by_an_interrupted_run_is_replaced(invoke, project, scw, gh):
    org, _, clock = scw
    assert invoke("bootstrap", "--env", "prod", "--json").exit_code == 0
    ids = dict(_facts(project).cloud_ids)
    # A run that created a deploy key and died before vecten.toml recorded it leaves a key
    # GitHub may hold.
    clock.advance(minutes=5)
    stray = org.client("iam").request(
        "POST", "/api-keys", json={"application_id": ids["app_deploy"], "expires_at": None}
    )["access_key"]
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    assert data["rotated"] == ["deploy_key"]
    assert data["environment_secrets"] == ENV_SECRETS
    assert data["deleted"] == []
    new = _facts(project).cloud_ids["key_deploy"]
    assert {k["access_key"] for k in org.keys_of(ids["app_deploy"])} == {
        stray,
        ids["key_deploy"],
        new,
    }
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["rotated"] == []
    assert doc(r)["deleted"] == ["deploy_retired_key", "deploy_retired_key"]
    (only,) = org.keys_of(ids["app_deploy"])
    assert only["access_key"] == new


def test_the_message_says_the_replaced_ci_keys_stay_until_the_next_bootstrap(
    invoke, project, scw, gh
):
    _, _, clock = scw
    assert invoke("bootstrap", "--env", "prod", "--json").exit_code == 0
    clock.advance(days=340)
    r = invoke("bootstrap", "--env", "prod")
    assert r.exit_code == 0, r.stdout
    assert "the replaced deploy and plan keys stay valid" in r.stdout
    assert "the next bootstrap deletes them" in r.stdout


def test_a_tfvars_region_other_than_the_rendered_one_refuses_before_any_write(
    invoke, project, scw, gh
):
    org, storage, _ = scw
    tfvars = project / "deploy" / "envs" / "prod.tfvars"
    text = re.sub(
        r"^region\s*=.*$", 'region = "nl-ams"', tfvars.read_text(encoding="utf-8"), flags=re.M
    )
    tfvars.write_text(text, encoding="utf-8")
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 6, r.stdout
    assert "'nl-ams'" in doc(r)["error"] and "'fr-par'" in doc(r)["error"]
    assert org.writes() == [] and storage.calls == []


def test_an_organization_maximum_duration_refusal_names_the_policy(invoke, project, scw, gh):
    org, _, _ = scw
    org.failures[("iam", "POST", "/api-keys")] = (400, "invalid_arguments")
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 3, r.stdout
    message = doc(r)["error"]
    assert "maximum duration" in message and "365 days" in message
    assert "HTTP 400 invalid_arguments" in message
    assert not _facts(project).bootstrapped


def test_a_stored_host_key_missing_from_iam_is_replaced(invoke, project, scw, gh):
    org, _, _ = scw
    assert invoke("bootstrap", "--env", "prod", "--json").exit_code == 0
    access, _ = _host_key(org, "mcp")
    del org.keys[access]
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["rotated"] == ["mcp_key"]
    assert _host_key(org, "mcp")[0] != access
    assert "retiring_mcp" not in _facts(project).cloud_ids


def test_without_scaleway_credentials_names_scw_init(invoke, project, scw, gh, monkeypatch):
    org, storage, _ = scw
    for name in ("SCW_ACCESS_KEY", "SCW_SECRET_KEY", "SCW_DEFAULT_ORGANIZATION_ID"):
        monkeypatch.delenv(name)
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 3, r.stdout
    assert "scw init" in doc(r)["error"]
    assert org.calls == [] and storage.calls == []
    assert gh.named("api") == [] and gh.named("secret") == []
    assert _facts(project).bootstrapped is False


def test_inside_actions_missing_keys_name_the_workflow_secrets(monkeypatch):
    from vecten.deploy.scaleway import clients

    for name in ("SCW_ACCESS_KEY", "SCW_SECRET_KEY", "SCW_DEFAULT_ORGANIZATION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(clients, "_from_config", lambda: {})
    with pytest.raises(Failure) as caught:
        clients.credentials(organization=False)
    message = caught.value.message
    assert "VECTEN_SCW_ACCESS_KEY" in message and "VECTEN_SCW_SECRET_KEY" in message
    assert "scw init" not in message


def test_a_rejected_key_names_scw_init_before_any_write(invoke, project, scw, gh):
    org, storage, _ = scw
    org.unauthorized = True
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 3, r.stdout
    error = doc(r)["error"]
    assert "scw init" in error and "401" in error
    assert org.writes() == [] and storage.calls == []
    assert gh.calls == []


def test_gh_logged_out_creates_nothing_in_scaleway(invoke, project, scw, monkeypatch):
    org, storage, _ = scw
    monkeypatch.setattr(gh_mod, "get_gh", lambda: FakeGh(logged_in=False))
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 3, r.stdout
    assert "gh auth login" in doc(r)["error"]
    assert org.writes() == [] and storage.calls == []


def test_a_failed_secret_set_records_no_fact(invoke, project, scw, monkeypatch):
    monkeypatch.setattr(gh_mod, "get_gh", lambda: FakeGh(failing=("secret",)))
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 5, r.stdout
    assert "gh secret set VECTEN_SCW_ACCESS_KEY --env prod failed" in doc(r)["error"]
    assert _facts(project).bootstrapped is False


def test_a_scaleway_error_reports_status_and_type_only(invoke, project, scw, gh):
    org, _, _ = scw
    org.failures[("iam", "POST", "/applications")] = (403, "permissions_denied")
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 5, r.stdout
    error = doc(r)["error"]
    assert "HTTP 403 permissions_denied" in error
    assert _facts(project).bootstrapped is False


def test_the_human_message_names_the_applications(invoke, project, scw, gh):
    r = invoke("bootstrap", "--env", "prod")
    assert r.exit_code == 0, r.stdout
    assert "deploy application" in r.stdout and "plan application" in r.stdout
    assert "host applications platform, mcp" in r.stdout
    assert "VECTEN_SCW_ACCESS_KEY" in r.stdout
    assert "role" not in r.stdout


def test_githubs_casing_is_used_without_a_note(invoke, project, scw, monkeypatch):
    gh = FakeGh(canonical="Acme/Data")
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    # Nothing on Scaleway trusts the repository by name, so its casing needs no note.
    assert data["repository"] == "Acme/Data" and "repository_note" not in data
    assert gh.named("api")[1] == ["api", "repos/Acme/Data/environments/prod"]


def _dns_rules(org) -> list[list[str]]:
    rules = org.rules_of_app("vecten-acme-prod-deploy")
    return [r["project_ids"] for r in rules if "DomainsDNSFullAccess" in r["permission_set_names"]]


def test_bootstrap_finds_the_dns_zone_in_another_project(invoke, project, scw, gh):
    org, _, _ = scw
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert _facts(project).cloud_ids["dns_project"] == DNS_PROJECT
    assert _dns_rules(org) == [[DNS_PROJECT]]
    # The zone is asked for by its full name, so a subzone of the same domain never answers.
    [lookup] = [c for c in org.calls if c[:3] == ("domain", "GET", "/dns-zones")][:1]
    assert lookup[4]["organization_id"] == ORGANIZATION
    assert lookup[4]["dns_zones"] == ["acme.com"]


def test_bootstrap_records_the_operator_for_the_artifacts_bucket_policy(invoke, project, scw, gh):
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert _facts(project).cloud_ids["operator_user"] == OPERATOR_USER
    assert "operator_application" not in _facts(project).cloud_ids


def test_bootstrap_finds_the_dns_zone_in_the_resources_project(invoke, project, scw, gh):
    org, _, _ = scw
    assert invoke("bootstrap", "--env", "prod", "--json").exit_code == 0
    resources = _facts(project).cloud_ids["project"]
    org.dns_zones[0]["project_id"] = resources
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert _facts(project).cloud_ids["dns_project"] == resources
    # One rule on the resources project then carries the DNS set beside the others.
    assert _dns_rules(org) == [[resources]]


def test_bootstrap_refuses_a_dns_zone_the_organization_lacks(invoke, project, scw, gh):
    org, storage, _ = scw
    org.dns_zones = [z for z in org.dns_zones if z["subdomain"]]
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 3, r.stdout
    error = doc(r)["error"]
    assert "acme.com" in error and "organization" in error
    assert org.writes() == [] and storage.calls == []
    assert _facts(project).bootstrapped is False


# --- the REST client ---------------------------------------------------------------------


def test_credentials_come_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("SCW_ACCESS_KEY", "SCWA")
    monkeypatch.setenv("SCW_SECRET_KEY", "s3cr3t")
    monkeypatch.setenv("SCW_DEFAULT_ORGANIZATION_ID", ORGANIZATION)
    monkeypatch.setenv("SCW_CONFIG_PATH", str(tmp_path / "none.yaml"))
    found = clients.credentials()
    assert (found.access_key, found.secret_key, found.organization_id) == (
        "SCWA",
        "s3cr3t",
        ORGANIZATION,
    )
    assert "s3cr3t" not in repr(found)


def test_credentials_fall_back_to_the_scw_config_profile(monkeypatch, tmp_path):
    for name in ("SCW_ACCESS_KEY", "SCW_SECRET_KEY", "SCW_DEFAULT_ORGANIZATION_ID", "SCW_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(
        "access_key: SCWTOP\nsecret_key: top-secret\ndefault_organization_id: org-top\n"
        "active_profile: work\nprofiles:\n  work:\n    access_key: SCWWORK\n"
        "    secret_key: work-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SCW_CONFIG_PATH", str(path))
    found = clients.credentials()
    assert (found.access_key, found.secret_key, found.organization_id) == (
        "SCWWORK",
        "work-secret",
        "org-top",
    )


def test_a_malformed_config_names_the_file_not_its_content(monkeypatch, tmp_path):
    for name in ("SCW_ACCESS_KEY", "SCW_SECRET_KEY", "SCW_DEFAULT_ORGANIZATION_ID"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("secret_key: [leaky-secret\n", encoding="utf-8")
    monkeypatch.setenv("SCW_CONFIG_PATH", str(path))
    with pytest.raises(Failure) as caught:
        clients.credentials()
    assert caught.value.code == ExitCode.CONFIG
    assert str(path) in caught.value.message
    assert "leaky-secret" not in caught.value.message


def test_missing_credentials_name_scw_init(monkeypatch, tmp_path):
    for name in ("SCW_ACCESS_KEY", "SCW_SECRET_KEY", "SCW_DEFAULT_ORGANIZATION_ID"):
        monkeypatch.delenv(name, raising=False)
    # On the laptop; inside GitHub Actions the message names the workflow's secrets instead.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("SCW_CONFIG_PATH", str(tmp_path / "none.yaml"))
    with pytest.raises(Failure) as caught:
        clients.credentials()
    assert caught.value.code == ExitCode.PREREQUISITE
    assert "scw init" in caught.value.message


@pytest.mark.parametrize(
    ("status", "named", "unnamed"),
    [
        (403, ("IAMReadOnly", "IAMManager", "bootstrap"), ("scw init",)),
        (401, ("scw init", "HTTP 401"), ("IAMReadOnly", "IAMManager")),
    ],
)
def test_the_login_check_names_what_the_key_lacks(status, named, unnamed):
    # destroy, add-admin and a laptop plan check the login too, and they need no IAMManager.
    class Iam:
        def request(self, method, path, **_):
            raise clients.ApiError(f"Scaleway iam {method} {path}", status, "permissions_denied")

    creds = clients.Credentials(OPERATOR_ACCESS, OPERATOR_SECRET, ORGANIZATION)
    with pytest.raises(Failure) as caught:
        bootstrap_mod.operator_principal(Iam(), creds)
    assert caught.value.code == ExitCode.PREREQUISITE
    for phrase in named:
        assert phrase in caught.value.message, phrase
    for phrase in unnamed:
        assert phrase not in caught.value.message, phrase
    assert OPERATOR_SECRET not in caught.value.message


def test_the_rest_client_sends_the_token_and_reports_status_and_type(monkeypatch, tmp_path):
    monkeypatch.setenv("SCW_ACCESS_KEY", "SCWA")
    monkeypatch.setenv("SCW_SECRET_KEY", "s3cr3t")
    monkeypatch.setenv("SCW_DEFAULT_ORGANIZATION_ID", ORGANIZATION)
    sent = []

    def fake_http(method, url, headers, body):
        sent.append((method, url, headers, body))
        if method == "DELETE":
            return 403, b'{"type": "permissions_denied", "message": "key SCWA s3cr3t"}'
        return 200, b'{"projects": [], "total_count": 0}'

    monkeypatch.setattr(clients, "_http", fake_http)
    api = clients.client("account")
    assert api.request("GET", "/projects", params={"name": "x", "ids": ["a", "b"]}) == {
        "projects": [],
        "total_count": 0,
    }
    method, url, headers, body = sent[0]
    assert url == "https://api.scaleway.com/account/v3/projects?name=x&ids=a&ids=b"
    assert headers["X-Auth-Token"] == "s3cr3t" and body is None
    with pytest.raises(clients.ApiError) as caught:
        api.request("DELETE", "/projects/p")
    assert caught.value.status == 403 and caught.value.kind == "permissions_denied"
    assert (
        caught.value.message
        == "Scaleway account DELETE /projects/p failed: HTTP 403 permissions_denied"
    )
    regional = clients.client("secret-manager", "fr-par")
    regional.request("POST", "/secrets", json={"a": 1})
    assert sent[-1][1] == "https://api.scaleway.com/secret-manager/v1beta1/regions/fr-par/secrets"
    assert json.loads(sent[-1][3]) == {"a": 1}
    assert sent[-1][2]["Content-Type"] == "application/json"


def test_the_rest_client_follows_no_redirect():
    import urllib.request

    opener = clients._opener()
    handlers = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
    assert handlers and all(
        h.redirect_request(None, None, 302, "", {}, "https://elsewhere") is None for h in handlers
    )


def test_an_unreachable_api_names_the_exception_type_only(monkeypatch):
    import http.client

    def broken(request, timeout):
        raise http.client.RemoteDisconnected("SCWA s3cr3t")

    monkeypatch.setattr(clients, "_open", broken)
    with pytest.raises(Failure) as caught:
        clients._http("GET", "https://api.scaleway.com/x", {}, None)
    assert caught.value.message.endswith("RemoteDisconnected")
    assert "s3cr3t" not in caught.value.message


def test_secret_values_are_stored_base64_in_the_deploy_scope(invoke, project, scw, gh):
    org, _, _ = scw
    assert invoke("bootstrap", "--env", "prod", "--json").exit_code == 0
    posts = [c for c in org.calls if c[0] == "secret-manager" and c[2].endswith("/versions")]
    assert len(posts) == 2
    for call in posts:
        access, secret = base64.b64decode(call[3]["data"]).decode().split(":")
        assert access.startswith("SCW") and secret in org.issued_secrets


# --- the pull-request plan ---------------------------------------------------------------


class _RecordingTerraform:
    def __init__(self):
        self.calls: list[list[str]] = []

    def which(self):
        return "/usr/local/bin/terraform"

    def run(self, args, cwd, env=None):
        import subprocess

        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "plan text\n", "")

    def plans(self) -> list[list[str]]:
        return [c for c in self.calls if c[:1] == ["plan"]]


def _planned_args(
    tmp_path, monkeypatch, cloud: str, *, cloud_only: bool, checks: list[int] | None = None
) -> list[list[str]]:
    from vecten.deploy.control import plan_environment
    from vecten.deploy.terraform import TerraformRoot

    from .deploy_fixtures import set_facts

    root = render_aws(tmp_path / f"{cloud}-{cloud_only}", cloud=cloud)
    set_facts(root, bootstrapped=True, state_bucket="vecten-acme-prod-state-11111111")
    if cloud == "scaleway":
        from vecten.deploy.scaleway import ScalewayProvider

        plane = ScalewayProvider(root, "prod")
    else:
        from vecten.deploy.aws import AwsProvider

        plane = AwsProvider(root, "prod")
    cloud_root = TerraformRoot(cloud, root / "deploy" / "terraform" / cloud, "k", "v.tfvars")
    monkeypatch.setattr(plane, "terraform_roots", lambda env, **kw: [cloud_root])
    monkeypatch.setattr(plane, "backend_config", lambda env, r: {"bucket": "b"})
    monkeypatch.setattr(plane, "terraform_env", lambda: {})
    terraform = _RecordingTerraform()
    # Each check records how many Terraform calls preceded it.
    seen = checks if checks is not None else []
    monkeypatch.setattr(plane, "check_refresh_safe", lambda: seen.append(len(terraform.calls)))
    out = tmp_path / f"out-{cloud}-{cloud_only}"
    out.mkdir()
    plan_environment(plane, "prod", terraform, out, cloud_only=cloud_only)
    return terraform.plans()


def test_the_scaleway_cloud_only_plan_does_not_refresh(tmp_path, monkeypatch):
    (plan,) = _planned_args(tmp_path, monkeypatch, "scaleway", cloud_only=True)
    assert "-refresh=false" in plan


def test_a_refreshing_plan_checks_once_before_terraform_and_a_cloud_only_one_never(
    tmp_path, monkeypatch
):
    checks: list[int] = []
    _planned_args(tmp_path, monkeypatch, "scaleway", cloud_only=False, checks=checks)
    assert checks == [0]
    checks.clear()
    _planned_args(tmp_path, monkeypatch, "scaleway", cloud_only=True, checks=checks)
    assert checks == []
    _planned_args(tmp_path, monkeypatch, "aws", cloud_only=True, checks=checks)
    assert checks == [0]


def test_full_plans_and_every_aws_plan_refresh(tmp_path, monkeypatch):
    (scaleway_full,) = _planned_args(tmp_path, monkeypatch, "scaleway", cloud_only=False)
    assert "-refresh=false" not in scaleway_full
    for cloud_only in (True, False):
        (aws,) = _planned_args(tmp_path, monkeypatch, "aws", cloud_only=cloud_only)
        assert not any(a.startswith("-refresh") for a in aws)


# --- projects are claimed by their marker --------------------------------------------------


def _add_env(project, env: str) -> None:
    prod = project / "deploy" / "envs" / "prod.tfvars"
    prod.with_name(f"{env}.tfvars").write_text(
        prod.read_text(encoding="utf-8").replace('"prod"', f'"{env}"'), encoding="utf-8"
    )


def test_an_environment_ending_in_state_is_refused(invoke, project, scw, gh):
    org, storage, _ = scw
    _add_env(project, "prod-state")
    assert invoke("bootstrap", "--env", "prod", "--json").exit_code == 0
    writes = len(org.writes())
    r = invoke("bootstrap", "--env", "prod-state", "--json")
    assert r.exit_code == 2, r.stdout
    assert "-state" in doc(r)["error"]
    assert org.writes()[writes:] == []
    assert load_config(project).environments["prod-state"].bootstrapped is False


def test_a_project_marked_for_another_role_is_refused(invoke, project, scw, gh):
    org, storage, _ = scw
    org.projects["0ff00000-0000-4000-8000-000000000099"] = {
        "id": "0ff00000-0000-4000-8000-000000000099",
        "name": "vecten-acme-prod-state",
        "organization_id": ORGANIZATION,
        "description": "vecten:resources:acme-prod:state",
    }
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 4, r.stdout
    error = doc(r)["error"]
    assert "vecten-acme-prod-state" in error
    assert "vecten:resources:acme-prod:state" in error and "vecten:state:acme:prod" in error
    assert org.app_named("vecten-acme-prod-deploy") is None
    assert storage.calls == []


def test_a_project_carrying_the_expected_marker_is_adopted(invoke, project, scw, gh):
    org, _, _ = scw
    existing = "0ff00000-0000-4000-8000-000000000042"
    org.projects[existing] = {
        "id": existing,
        "name": "vecten-acme-prod",
        "organization_id": ORGANIZATION,
        "description": "vecten:resources:acme:prod",
    }
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    assert data["project_id"] == existing and "project" not in data["created"]
    state = org.projects[data["state_project_id"]]
    assert state["description"] == "vecten:state:acme:prod"
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0 and doc(r)["created"] == []


# --- a state bucket whose policy names another operator ---------------------------------


def test_a_forbidden_state_bucket_is_reclaimed_by_rewriting_its_policy(invoke, project, scw, gh):
    org, storage, _ = scw
    assert invoke("bootstrap", "--env", "prod", "--json").exit_code == 0
    bucket = load_config(project).environments["prod"].state_bucket
    storage.forbidden_heads.add(bucket)
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert len(storage.named("create_bucket")) == 1
    assert len(storage.named("put_bucket_policy")) == 2
    assert "state_bucket" not in doc(r)["created"]


def test_a_forbidden_state_bucket_whose_policy_cannot_be_rewritten_names_the_owner(
    invoke, project, scw, gh
):
    org, storage, _ = scw
    assert invoke("bootstrap", "--env", "prod", "--json").exit_code == 0
    bucket = load_config(project).environments["prod"].state_bucket
    storage.forbidden_heads.add(bucket)
    storage.policy_denied.add(bucket)
    r = invoke("bootstrap", "--env", "prod", "--json")
    assert r.exit_code == 3, r.stdout
    error = doc(r)["error"]
    assert bucket in error and "OperatorFullAccess" in error and "Owner" in error
    assert storage.named("put_bucket_versioning")[-1][2]["Bucket"] == bucket
    assert len(storage.named("put_bucket_versioning")) == 1


# --- the CLI holds the provider slot ------------------------------------------------------


def test_the_runtime_class_refuses_writes_the_cli_class_allows(project, monkeypatch):
    from vecten.deploy import control
    from vecten.deploy.scaleway import ScalewayProvider
    from vecten_shared import provider as provider_module
    from vecten_shared.provider import get_provider
    from vecten_shared.provider.scaleway import ScalewayRuntimeProvider

    monkeypatch.setitem(provider_module._REGISTRY, "scaleway", ScalewayRuntimeProvider)
    runtime = get_provider("scaleway", project, "prod")
    assert type(runtime) is ScalewayRuntimeProvider
    assert runtime.may_write("MCP_IMPERSONATOR_PASSWORD")
    assert not runtime.may_write("HUBSPOT_API_KEY")
    control.register_cli_provider("scaleway")
    cli = get_provider("scaleway", project, "prod")
    assert type(cli) is ScalewayProvider and cli.may_write("HUBSPOT_API_KEY")


def test_registration_keeps_another_scaleway_factory(project, monkeypatch):
    from vecten.deploy import scaleway as deploy_scaleway
    from vecten_shared import provider as provider_module
    from vecten_shared.provider.scaleway import ScalewayRuntimeProvider

    class Other(ScalewayRuntimeProvider):
        pass

    monkeypatch.setitem(provider_module._REGISTRY, "scaleway", Other)
    deploy_scaleway.register()
    assert provider_module._REGISTRY["scaleway"] is Other


def test_mcp_can_write_only_scoped_standing_identity_secrets():
    rules = _by_sets(host_rules(STANDARD, "acme", "prod", RESOURCES, "mcp"))
    condition = rules[("SecretManagerSecretCreate",)]["condition"]
    assert f'resource.name == "{ROOT}/mcp/MCP_SIGNING_KEY"' in condition
    assert "MCP_USER_" not in condition
