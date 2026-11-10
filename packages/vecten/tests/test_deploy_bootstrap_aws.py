import json
from pathlib import Path
from urllib.parse import unquote

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from vecten.deploy.aws.bootstrap import (
    GITHUB_OIDC_URL,
    POWER_USER_ARN,
    boundary_policy,
    caller_account,
    ensure_boundary_policy,
    ensure_deploy_role,
    ensure_oidc_provider,
    ensure_plan_role,
    ensure_state_bucket,
)
from vecten.deploy.github_env import (
    canonical_repository,
    default_branch,
    ensure_environment,
    oidc_subject_prefixes,
    repository_facts,
    repository_ids,
    set_environment_variables,
    set_repository_variables,
)
from vecten_shared.output import ExitCode, Failure

from .deploy_fixtures import (
    MANAGED_POLICIES,
    OWNER_ID,
    REGION,
    REPOSITORY_ID,
    STATE_BUCKET,
    FakeGh,
    aws_credentials,
    no_aws_credentials,
)

ACCOUNT = "123456789012"
LEGACY = "repo:acme/data"
IMMUTABLE = f"repo:acme@{OWNER_ID}/data@{REPOSITORY_ID}"
PREFIXES = (LEGACY, IMMUTABLE)
ROLE = "vecten-acme-prod-deploy"
PLAN_ROLE = "vecten-acme-prod-plan"
BOUNDARY = f"arn:aws:iam::{ACCOUNT}:policy/vecten-acme-prod-boundary"
HOSTS = ("platform", "mcp", "main")


@pytest.fixture
def aws(tmp_path, monkeypatch):
    aws_credentials(monkeypatch, tmp_path)
    with mock_aws(config=MANAGED_POLICIES):
        yield


def _iam():
    return boto3.client("iam", region_name=REGION)


def _s3(region=REGION):
    return boto3.client("s3", region_name=region)


def _oidc_providers(iam):
    return [p["Arn"] for p in iam.list_open_id_connect_providers()["OpenIDConnectProviderList"]]


# State bucket


def test_state_bucket_is_private_versioned_and_encrypted(aws):
    s3 = _s3()
    assert ensure_state_bucket(s3, STATE_BUCKET, REGION) is True
    location = s3.get_bucket_location(Bucket=STATE_BUCKET)["LocationConstraint"]
    assert location == REGION
    assert s3.get_bucket_versioning(Bucket=STATE_BUCKET)["Status"] == "Enabled"
    rules = s3.get_bucket_encryption(Bucket=STATE_BUCKET)["ServerSideEncryptionConfiguration"]
    algorithm = rules["Rules"][0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
    assert algorithm == "AES256"
    block = s3.get_public_access_block(Bucket=STATE_BUCKET)["PublicAccessBlockConfiguration"]
    assert block == {
        "BlockPublicAcls": True,
        "IgnorePublicAcls": True,
        "BlockPublicPolicy": True,
        "RestrictPublicBuckets": True,
    }
    policy = json.loads(s3.get_bucket_policy(Bucket=STATE_BUCKET)["Policy"])
    (deny,) = policy["Statement"]
    assert deny["Effect"] == "Deny"
    assert deny["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    assert set(deny["Resource"]) == {
        f"arn:aws:s3:::{STATE_BUCKET}",
        f"arn:aws:s3:::{STATE_BUCKET}/*",
    }


def test_existing_state_bucket_is_not_recreated_and_is_hardened(aws):
    s3 = _s3()
    s3.create_bucket(Bucket=STATE_BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
    assert ensure_state_bucket(s3, STATE_BUCKET, REGION) is False
    assert s3.get_bucket_versioning(Bucket=STATE_BUCKET)["Status"] == "Enabled"
    block = s3.get_public_access_block(Bucket=STATE_BUCKET)["PublicAccessBlockConfiguration"]
    assert all(block.values())


def test_state_bucket_in_us_east_1_has_no_location_constraint(aws):
    s3 = _s3("us-east-1")
    assert ensure_state_bucket(s3, STATE_BUCKET, "us-east-1") is True
    assert s3.get_bucket_location(Bucket=STATE_BUCKET)["LocationConstraint"] in (None, "")


def test_state_bucket_failure_names_the_operation_type_and_code_only():
    class Refusing:
        def head_bucket(self, **kwargs):
            raise ClientError({"Error": {"Code": "403", "Message": "secret=x"}}, "HeadBucket")

    with pytest.raises(Failure) as caught:
        ensure_state_bucket(Refusing(), STATE_BUCKET, REGION)
    assert caught.value.code == ExitCode.TOOL_FAILED
    assert caught.value.message == "S3 HeadBucket failed: ClientError 403"
    assert "secret=x" not in caught.value.message


def test_aws_failure_appends_a_plain_error_code_and_never_the_message():
    from botocore.exceptions import EndpointConnectionError

    from vecten.deploy.aws.clients import aws_failure

    denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "key=s3cr3t"}}, "PutObject")
    assert aws_failure("S3 PutObject", denied).message == (
        "S3 PutObject failed: ClientError AccessDenied"
    )
    odd = ClientError({"Error": {"Code": "x y s3cr3t", "Message": "s3cr3t"}}, "PutObject")
    assert aws_failure("S3 PutObject", odd).message == "S3 PutObject failed: ClientError"
    offline = EndpointConnectionError(endpoint_url="https://s3.example/?s3cr3t")
    assert aws_failure("S3 PutObject", offline).message == (
        "S3 PutObject failed: EndpointConnectionError"
    )


# OIDC provider


def test_oidc_provider_is_created_once(aws):
    iam = _iam()
    arn = ensure_oidc_provider(iam)
    assert arn == f"arn:aws:iam::{ACCOUNT}:oidc-provider/{GITHUB_OIDC_URL}"
    found = iam.get_open_id_connect_provider(OpenIDConnectProviderArn=arn)
    assert found["ClientIDList"] == ["sts.amazonaws.com"]
    assert found["ThumbprintList"]
    assert ensure_oidc_provider(iam) == arn
    assert _oidc_providers(iam) == [arn]


def test_oidc_provider_of_another_project_is_reused(aws):
    iam = _iam()
    other = iam.create_open_id_connect_provider(
        Url=f"https://{GITHUB_OIDC_URL}",
        ClientIDList=["sts.amazonaws.com"],
        ThumbprintList=["6938fd4d98bab03faadb97b34396831e3780aea1"],
    )["OpenIDConnectProviderArn"]
    created: list[str] = []
    assert ensure_oidc_provider(iam, created=created) == other
    assert created == []
    assert _oidc_providers(iam) == [other]


class ProviderWithoutStsAudience:
    """moto does not implement AddClientIDToOpenIDConnectProvider."""

    arn = f"arn:aws:iam::{ACCOUNT}:oidc-provider/{GITHUB_OIDC_URL}"

    def __init__(self):
        self.client_ids = ["https://github.com/acme"]
        self.created = 0

    def list_open_id_connect_providers(self):
        return {"OpenIDConnectProviderList": [{"Arn": self.arn}]}

    def get_open_id_connect_provider(self, OpenIDConnectProviderArn):
        return {"ClientIDList": list(self.client_ids)}

    def add_client_id_to_open_id_connect_provider(self, OpenIDConnectProviderArn, ClientID):
        assert OpenIDConnectProviderArn == self.arn
        self.client_ids.append(ClientID)

    def create_open_id_connect_provider(self, **kwargs):
        self.created += 1
        return {"OpenIDConnectProviderArn": self.arn}


def test_reused_oidc_provider_gains_the_sts_audience():
    iam = ProviderWithoutStsAudience()
    assert ensure_oidc_provider(iam) == iam.arn
    assert iam.client_ids == ["https://github.com/acme", "sts.amazonaws.com"]
    assert iam.created == 0


def test_other_oidc_providers_are_ignored(aws):
    iam = _iam()
    iam.create_open_id_connect_provider(
        Url="https://gitlab.com", ClientIDList=["sts.amazonaws.com"], ThumbprintList=["a" * 40]
    )
    created: list[str] = []
    arn = ensure_oidc_provider(iam, created=created)
    assert arn.endswith(f"oidc-provider/{GITHUB_OIDC_URL}")
    assert created == ["oidc_provider"]


# Deploy role


def _role(iam, role=ROLE):
    return iam.get_role(RoleName=role)["Role"]


def _trust(iam, role=ROLE):
    document = _role(iam, role)["AssumeRolePolicyDocument"]
    return json.loads(document) if isinstance(document, str) else document


def _inline(iam, role=ROLE):
    (name,) = iam.list_role_policies(RoleName=role)["PolicyNames"]
    document = iam.get_role_policy(RoleName=role, PolicyName=name)["PolicyDocument"]
    return json.loads(document) if isinstance(document, str) else document


def test_deploy_role_trusts_only_the_environment_subject(aws):
    iam = _iam()
    oidc = ensure_oidc_provider(iam)
    arn = ensure_deploy_role(
        iam, ROLE, oidc, "acme/data", "prod", "acme", subject_prefixes=PREFIXES
    )
    assert arn == f"arn:aws:iam::{ACCOUNT}:role/{ROLE}"
    (statement,) = _trust(iam)["Statement"]
    assert statement["Effect"] == "Allow"
    assert statement["Principal"] == {"Federated": oidc}
    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert statement["Condition"] == {
        "StringEquals": {
            f"{GITHUB_OIDC_URL}:aud": "sts.amazonaws.com",
            f"{GITHUB_OIDC_URL}:sub": [
                f"{LEGACY}:environment:prod",
                f"{IMMUTABLE}:environment:prod",
            ],
        }
    }


HOST_ROLES = [f"arn:aws:iam::{ACCOUNT}:role/vecten-acme-prod-{h}" for h in HOSTS]
HOST_PROFILES = [f"arn:aws:iam::{ACCOUNT}:instance-profile/vecten-acme-prod-{h}" for h in HOSTS]


def _statements(iam, role=ROLE):
    return {s["Sid"]: s for s in _inline(iam, role)["Statement"]}


def _as_list(value):
    return value if isinstance(value, list) else [value]


def _deploy_statements(iam):
    oidc = ensure_oidc_provider(iam)
    ensure_deploy_role(iam, ROLE, oidc, "acme/data", "prod", "acme", subject_prefixes=PREFIXES)
    return _statements(iam)


def test_deploy_role_holds_power_user_and_one_inline_policy(aws):
    iam = _iam()
    _deploy_statements(iam)
    attached = iam.list_attached_role_policies(RoleName=ROLE)["AttachedPolicies"]
    assert [p["PolicyArn"] for p in attached] == [POWER_USER_ARN]
    assert len(iam.list_role_policies(RoleName=ROLE)["PolicyNames"]) == 1


def test_role_writes_require_the_boundary(aws):
    bounded = _deploy_statements(_iam())["BoundedRoleWrites"]
    assert bounded["Effect"] == "Allow"
    assert set(bounded["Action"]) == {
        "iam:CreateRole",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:DetachRolePolicy",
    }
    assert sorted(bounded["Resource"]) == sorted(HOST_ROLES)
    assert bounded["Condition"] == {"StringEquals": {"iam:PermissionsBoundary": BOUNDARY}}


def test_attach_allows_only_the_ssm_core_policy_under_the_boundary(aws):
    attach = _deploy_statements(_iam())["AttachSsmCore"]
    assert attach["Action"] == "iam:AttachRolePolicy"
    assert sorted(attach["Resource"]) == sorted(HOST_ROLES)
    assert attach["Condition"] == {
        "StringEquals": {"iam:PermissionsBoundary": BOUNDARY},
        "ArnEquals": {"iam:PolicyARN": "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"},
    }


def test_ci_manages_no_customer_managed_policy(aws):
    statements = _deploy_statements(_iam())
    for statement in statements.values():
        if statement["Effect"] != "Allow":
            continue
        for resource in _as_list(statement["Resource"]):
            assert ":policy/" not in resource, statement["Sid"]
        assert not {"iam:CreatePolicy", "iam:CreatePolicyVersion"} & set(
            _as_list(statement["Action"])
        )


def test_pass_role_names_the_host_roles_without_a_service_condition(aws):
    """AddRoleToInstanceProfile checks PassRole without `iam:PassedToService`."""
    passing = _deploy_statements(_iam())["PassHostRoles"]
    assert passing["Action"] == "iam:PassRole"
    assert sorted(passing["Resource"]) == sorted(HOST_ROLES)
    assert "Condition" not in passing


def test_unconditioned_role_actions_exclude_the_bounded_ones(aws):
    statements = _deploy_statements(_iam())
    manage = statements["ManageHostRoles"]
    assert "Condition" not in manage
    assert not {
        "iam:CreateRole",
        "iam:PutRolePolicy",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:PassRole",
        "iam:PutRolePermissionsBoundary",
        "iam:DeleteRolePermissionsBoundary",
    } & set(manage["Action"])
    assert sorted(manage["Resource"]) == sorted(HOST_ROLES)
    profiles = statements["ManageHostInstanceProfiles"]
    assert sorted(profiles["Resource"]) == sorted(HOST_PROFILES)
    assert "iam:AddRoleToInstanceProfile" in profiles["Action"]


def test_boundaries_cannot_be_changed_or_removed(aws):
    statements = _deploy_statements(_iam())
    keep = statements["KeepRoleBoundaries"]
    assert keep["Effect"] == "Deny"
    assert set(keep["Action"]) == {
        "iam:PutRolePermissionsBoundary",
        "iam:DeleteRolePermissionsBoundary",
    }
    assert set(HOST_ROLES) <= set(keep["Resource"])
    protect = statements["ProtectBoundaryPolicy"]
    assert protect["Effect"] == "Deny"
    assert set(protect["Action"]) == {
        "iam:CreatePolicyVersion",
        "iam:DeletePolicy",
        "iam:DeletePolicyVersion",
        "iam:SetDefaultPolicyVersion",
    }
    assert BOUNDARY in protect["Resource"]


def test_the_deploy_and_plan_roles_cannot_change_themselves(aws):
    own = _deploy_statements(_iam())["NotItsOwnRole"]
    assert own["Effect"] == "Deny"
    assert sorted(own["Resource"]) == sorted(
        [f"arn:aws:iam::{ACCOUNT}:role/{ROLE}", f"arn:aws:iam::{ACCOUNT}:role/{PLAN_ROLE}"]
    )
    assert {
        "iam:PutRolePolicy",
        "iam:AttachRolePolicy",
        "iam:UpdateAssumeRolePolicy",
        "iam:PassRole",
    } <= set(own["Action"])


def test_no_iam_allow_reaches_another_environments_names(aws):
    """`prod` must never match `prod-eu`'s roles, profiles or boundary."""
    import fnmatch

    statements = _deploy_statements(_iam())
    neighbours = [
        f"arn:aws:iam::{ACCOUNT}:{kind}/vecten-acme-prod-eu-{host}"
        for kind in ("role", "instance-profile")
        for host in HOSTS
    ] + [f"arn:aws:iam::{ACCOUNT}:role/vecten-acme-prod-mainland-{h}" for h in HOSTS]
    for statement in statements.values():
        if statement["Effect"] != "Allow":
            continue
        for pattern in _as_list(statement["Resource"]):
            for arn in neighbours:
                assert not fnmatch.fnmatchcase(arn, pattern), (statement["Sid"], pattern, arn)


def test_role_resources_name_the_module_host_names(aws):
    """The module names host roles and instance profiles `<name_prefix>-<host key>`."""
    statements = _deploy_statements(_iam())
    for sid in ("ManageHostRoles", "BoundedRoleWrites", "AttachSsmCore", "PassHostRoles"):
        assert set(_as_list(statements[sid]["Resource"])) == set(HOST_ROLES)


# Permissions boundary


def _boundary_document(iam, arn=BOUNDARY):
    policy = iam.get_policy(PolicyArn=arn)["Policy"]
    version = iam.get_policy_version(PolicyArn=arn, VersionId=policy["DefaultVersionId"])
    document = version["PolicyVersion"]["Document"]
    return json.loads(unquote(document)) if isinstance(document, str) else document


def test_boundary_allows_only_what_host_roles_need(aws):
    iam = _iam()
    created: list[str] = []
    assert ensure_boundary_policy(iam, ACCOUNT, "acme", "prod", created=created) == BOUNDARY
    assert created == ["boundary_policy"]
    statements = {s["Sid"]: s for s in _boundary_document(iam)["Statement"]}
    assert all(s["Effect"] == "Allow" for s in statements.values())
    secrets = statements["EnvironmentSecrets"]
    assert set(secrets["Action"]) == {
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret",
        "secretsmanager:CreateSecret",
        "secretsmanager:PutSecretValue",
    }
    assert secrets["Resource"] == [
        f"arn:aws:secretsmanager:*:{ACCOUNT}:secret:vecten/acme/prod/platform/*",
        f"arn:aws:secretsmanager:*:{ACCOUNT}:secret:vecten/acme/prod/mcp/*",
    ]
    resources = [r for s in statements.values() for r in _as_list(s["Resource"])]
    assert not any("/deploy/" in r or "/admin/" in r for r in resources)
    kms = statements["SecretsManagerKeys"]
    assert set(kms["Action"]) == {"kms:Decrypt", "kms:GenerateDataKey"}
    assert kms["Resource"] == "*"
    assert kms["Condition"] == {"StringLike": {"kms:ViaService": "secretsmanager.*.amazonaws.com"}}
    masters = statements["RdsMasterSecrets"]
    assert set(masters["Action"]) == {
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret",
    }
    assert masters["Resource"] == f"arn:aws:secretsmanager:*:{ACCOUNT}:secret:rds!*"
    artifacts = f"arn:aws:s3:::vecten-acme-prod-artifacts-{ACCOUNT}"
    assert statements["ArtifactObjects"]["Resource"] == f"{artifacts}/*"
    assert set(statements["ArtifactObjects"]["Action"]) == {"s3:GetObject", "s3:PutObject"}
    assert statements["ArtifactBucket"] == {
        "Sid": "ArtifactBucket",
        "Effect": "Allow",
        "Action": "s3:ListBucket",
        "Resource": artifacts,
    }
    ssm = set(statements["SsmAgent"]["Action"])
    assert {
        "ssm:UpdateInstanceInformation",
        "ssmmessages:*",
        "ec2messages:*",
        "ssm:ListInstanceAssociations",
        "ssm:GetDocument",
        "ssm:PutInventory",
        "ssm:UpdateInstanceAssociationStatus",
    } <= ssm
    # A host identity reads no Parameter Store value, which may belong to another workload.
    assert not {a for a in ssm if a.startswith("ssm:GetParameter")}
    assert not any(a.startswith("iam:") for s in statements.values() for a in _as_list(s["Action"]))
    assert {"logs:CreateLogStream", "logs:PutLogEvents"} <= set(statements["Logs"]["Action"])


def test_the_terraform_drift_fixture_is_the_boundary_bootstrap_creates():
    """terraform/aws/tests/boundary.tftest.hcl checks every host scope policy against it."""
    fixture = Path(__file__).resolve().parents[3] / "terraform/aws/tests/boundary.json"
    assert json.loads(fixture.read_text(encoding="utf-8")) == boundary_policy(
        ACCOUNT, "acme", "prod"
    )


def test_boundary_is_not_versioned_again_when_unchanged(aws):
    iam = _iam()
    ensure_boundary_policy(iam, ACCOUNT, "acme", "prod")
    created: list[str] = []
    assert ensure_boundary_policy(iam, ACCOUNT, "acme", "prod", created=created) == BOUNDARY
    assert created == []
    assert len(iam.list_policy_versions(PolicyArn=BOUNDARY)["Versions"]) == 1


def test_a_changed_boundary_gets_a_new_default_version(aws):
    iam = _iam()
    stale = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}],
    }
    iam.create_policy(PolicyName="vecten-acme-prod-boundary", PolicyDocument=json.dumps(stale))
    ensure_boundary_policy(iam, ACCOUNT, "acme", "prod")
    versions = iam.list_policy_versions(PolicyArn=BOUNDARY)["Versions"]
    assert len(versions) == 2
    assert "EnvironmentSecrets" in {s["Sid"] for s in _boundary_document(iam)["Statement"]}


def test_a_changed_boundary_prunes_the_oldest_version_at_the_limit(aws):
    iam = _iam()

    def stale(n):
        return json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Action": f"s3:Get{n}", "Resource": "*"}],
            }
        )

    iam.create_policy(PolicyName="vecten-acme-prod-boundary", PolicyDocument=stale(1))
    for n in range(2, 6):
        iam.create_policy_version(PolicyArn=BOUNDARY, PolicyDocument=stale(n), SetAsDefault=n == 5)
    ensure_boundary_policy(iam, ACCOUNT, "acme", "prod")
    versions = iam.list_policy_versions(PolicyArn=BOUNDARY)["Versions"]
    assert len(versions) == 5
    assert "v1" not in {v["VersionId"] for v in versions}
    assert "EnvironmentSecrets" in {s["Sid"] for s in _boundary_document(iam)["Statement"]}


# Plan role


def test_plan_role_trusts_pull_requests_and_the_default_branch(aws):
    iam = _iam()
    oidc = ensure_oidc_provider(iam)
    created: list[str] = []
    arn = ensure_plan_role(
        iam,
        PLAN_ROLE,
        oidc,
        "acme/data",
        "main",
        STATE_BUCKET,
        "prod",
        "acme",
        subject_prefixes=PREFIXES,
        created=created,
    )
    assert arn == f"arn:aws:iam::{ACCOUNT}:role/{PLAN_ROLE}"
    assert created == ["plan_role"]
    (statement,) = _trust(iam, PLAN_ROLE)["Statement"]
    assert statement["Principal"] == {"Federated": oidc}
    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert statement["Condition"] == {
        "StringEquals": {
            f"{GITHUB_OIDC_URL}:aud": "sts.amazonaws.com",
            f"{GITHUB_OIDC_URL}:sub": [
                f"{LEGACY}:pull_request",
                f"{LEGACY}:ref:refs/heads/main",
                f"{IMMUTABLE}:pull_request",
                f"{IMMUTABLE}:ref:refs/heads/main",
            ],
        }
    }


def test_plan_role_holds_no_managed_policy(aws):
    iam = _iam()
    oidc = ensure_oidc_provider(iam)
    ensure_plan_role(
        iam,
        PLAN_ROLE,
        oidc,
        "acme/data",
        "main",
        STATE_BUCKET,
        "prod",
        "acme",
        subject_prefixes=PREFIXES,
    )
    assert iam.list_attached_role_policies(RoleName=PLAN_ROLE)["AttachedPolicies"] == []
    assert len(iam.list_role_policies(RoleName=PLAN_ROLE)["PolicyNames"]) == 1


def test_plan_role_loses_a_managed_policy_attached_earlier(aws):
    iam = _iam()
    oidc = ensure_oidc_provider(iam)
    ensure_plan_role(
        iam,
        PLAN_ROLE,
        oidc,
        "acme/data",
        "main",
        STATE_BUCKET,
        "prod",
        "acme",
        subject_prefixes=PREFIXES,
    )
    iam.attach_role_policy(RoleName=PLAN_ROLE, PolicyArn="arn:aws:iam::aws:policy/ReadOnlyAccess")
    ensure_plan_role(
        iam,
        PLAN_ROLE,
        oidc,
        "acme/data",
        "main",
        STATE_BUCKET,
        "prod",
        "acme",
        subject_prefixes=PREFIXES,
    )
    assert iam.list_attached_role_policies(RoleName=PLAN_ROLE)["AttachedPolicies"] == []


def test_plan_role_reads_only_what_a_plan_refreshes(aws):
    iam = _iam()
    oidc = ensure_oidc_provider(iam)
    ensure_plan_role(
        iam,
        PLAN_ROLE,
        oidc,
        "acme/data",
        "main",
        STATE_BUCKET,
        "prod",
        "acme",
        subject_prefixes=PREFIXES,
    )
    statements = _statements(iam, PLAN_ROLE)
    infrastructure = statements["ReadInfrastructure"]
    assert infrastructure["Resource"] == "*"
    assert set(infrastructure["Action"]) == {
        "ec2:Describe*",
        "rds:Describe*",
        "rds:ListTagsForResource",
        "iam:Get*",
        "iam:List*",
        "route53:Get*",
        "route53:List*",
        "sts:GetCallerIdentity",
    }
    parameters = statements["ReadAmiParameters"]
    assert set(parameters["Action"]) == {"ssm:GetParameter", "ssm:GetParameters"}
    assert parameters["Resource"] == "arn:aws:ssm:*::parameter/aws/service/*"
    artifacts = f"arn:aws:s3:::vecten-acme-prod-artifacts-{ACCOUNT}"
    bucket = statements["ReadArtifactsBucket"]
    assert set(bucket["Action"]) == {"s3:Get*", "s3:List*"}
    assert bucket["Resource"] == artifacts
    assert set(statements) == {
        "ReadInfrastructure",
        "ReadAmiParameters",
        "ReadArtifactsBucket",
        "ReadState",
        "StateLocks",
    }
    assert all(s["Effect"] == "Allow" for s in statements.values())
    assert not any(
        a.startswith("s3:") and r == "*"
        for s in statements.values()
        for a in _as_list(s["Action"])
        for r in _as_list(s["Resource"])
    )


def test_plan_role_writes_only_lock_files(aws):
    iam = _iam()
    oidc = ensure_oidc_provider(iam)
    ensure_plan_role(
        iam,
        PLAN_ROLE,
        oidc,
        "acme/data",
        "main",
        STATE_BUCKET,
        "prod",
        "acme",
        subject_prefixes=PREFIXES,
    )
    statements = _statements(iam, PLAN_ROLE)
    writes = {
        a
        for s in statements.values()
        for a in _as_list(s["Action"])
        if not a.split(":")[1].startswith(("Get", "List", "Describe"))
    }
    assert writes == {"s3:PutObject", "s3:DeleteObject"}
    assert set(statements["ReadState"]["Action"]) == {"s3:GetObject", "s3:ListBucket"}
    assert set(statements["ReadState"]["Resource"]) == {
        f"arn:aws:s3:::{STATE_BUCKET}",
        f"arn:aws:s3:::{STATE_BUCKET}/*",
    }
    assert set(statements["StateLocks"]["Action"]) == {"s3:PutObject", "s3:DeleteObject"}
    assert statements["StateLocks"]["Resource"] == f"arn:aws:s3:::{STATE_BUCKET}/prod/*.tflock"
    created: list[str] = []
    ensure_plan_role(
        iam,
        PLAN_ROLE,
        oidc,
        "acme/data",
        "trunk",
        STATE_BUCKET,
        "prod",
        "acme",
        subject_prefixes=PREFIXES,
        created=created,
    )
    assert created == []
    (statement,) = _trust(iam, PLAN_ROLE)["Statement"]
    subjects = statement["Condition"]["StringEquals"][f"{GITHUB_OIDC_URL}:sub"]
    assert f"{LEGACY}:ref:refs/heads/trunk" in subjects
    assert f"{IMMUTABLE}:ref:refs/heads/trunk" in subjects


def test_existing_role_trust_is_updated_in_place(aws):
    iam = _iam()
    oidc = ensure_oidc_provider(iam)
    first = ensure_deploy_role(
        iam, ROLE, oidc, "acme/old", "prod", "acme", subject_prefixes=PREFIXES
    )
    role_id = _role(iam)["RoleId"]
    created: list[str] = []
    again = ensure_deploy_role(
        iam, ROLE, oidc, "acme/data", "prod", "acme", subject_prefixes=PREFIXES, created=created
    )
    assert again == first
    assert created == []
    assert _role(iam)["RoleId"] == role_id
    (statement,) = _trust(iam)["Statement"]
    sub = statement["Condition"]["StringEquals"][f"{GITHUB_OIDC_URL}:sub"]
    assert sub == [f"{LEGACY}:environment:prod", f"{IMMUTABLE}:environment:prod"]
    assert len(iam.list_attached_role_policies(RoleName=ROLE)["AttachedPolicies"]) == 1
    assert len(iam.list_role_policies(RoleName=ROLE)["PolicyNames"]) == 1


# Caller identity


def test_caller_account_reads_sts(aws):
    assert caller_account(boto3.client("sts", region_name=REGION)) == ACCOUNT


def test_caller_account_without_credentials_names_the_logins(tmp_path, monkeypatch):
    no_aws_credentials(monkeypatch, tmp_path)
    with pytest.raises(Failure) as caught:
        caller_account(boto3.client("sts", region_name=REGION))
    assert caught.value.code == ExitCode.PREREQUISITE
    assert "aws sso login" in caught.value.message
    assert "aws configure" in caught.value.message


@pytest.mark.parametrize("code", ["ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId"])
def test_caller_account_with_an_expired_token_names_the_logins(code):
    class Expired:
        def get_caller_identity(self):
            raise ClientError({"Error": {"Code": code, "Message": "x"}}, "GetCallerIdentity")

    with pytest.raises(Failure) as caught:
        caller_account(Expired())
    assert caught.value.code == ExitCode.PREREQUISITE
    assert "aws sso login" in caught.value.message


def test_caller_account_with_an_expired_sso_session_names_the_logins():
    from botocore.exceptions import TokenRetrievalError

    class Expired:
        def get_caller_identity(self):
            raise TokenRetrievalError(provider="sso", error_msg="token expired")

    with pytest.raises(Failure) as caught:
        caller_account(Expired())
    assert caught.value.code == ExitCode.PREREQUISITE
    assert "aws sso login" in caught.value.message


# GitHub environment variables


ENV_PATH = "repos/acme/data/environments/prod"
RESTRICT = [
    "-F",
    "deployment_branch_policy[protected_branches]=false",
    "-F",
    "deployment_branch_policy[custom_branch_policies]=true",
]


def test_a_new_environment_is_restricted_to_the_default_branch(tmp_path):
    gh = FakeGh()
    assert ensure_environment(gh, "acme/data", "prod", "main", cwd=tmp_path) == "default-branch"
    assert gh.calls == [
        ["api", ENV_PATH],
        ["api", "-X", "PUT", ENV_PATH, *RESTRICT],
        [
            "api",
            "-X",
            "POST",
            f"{ENV_PATH}/deployment-branch-policies",
            "-f",
            "name=main",
            "-f",
            "type=branch",
        ],
    ]


def test_an_existing_environment_is_never_put_again(tmp_path):
    gh = FakeGh(environments=(ENV_PATH,))
    assert ensure_environment(gh, "acme/data", "prod", "main", cwd=tmp_path) == "unchanged"
    assert gh.calls == [["api", ENV_PATH]]


@pytest.mark.parametrize(
    "policy",
    [
        {"protected_branches": True, "custom_branch_policies": False},
        {"protected_branches": False, "custom_branch_policies": True},
    ],
    ids=["protected-branches", "custom-policies"],
)
def test_an_existing_environment_under_a_branch_restriction_is_unchanged(tmp_path, policy):
    # Custom policies admit only what they name, so an empty list admits no branch at all.
    for branches in ([], ["main"]):
        gh = FakeGh(environment_policies={ENV_PATH: (policy, list(branches))})
        assert ensure_environment(gh, "acme/data", "prod", "main", cwd=tmp_path) == "unchanged"
        assert gh.calls == [["api", ENV_PATH]]


def test_an_existing_environment_open_to_every_branch_is_unrestricted(tmp_path):
    gh = FakeGh(environment_policies={ENV_PATH: (None, [])})
    assert ensure_environment(gh, "acme/data", "prod", "main", cwd=tmp_path) == "unrestricted"
    # An existing environment is a human's to change; vecten only reads it.
    assert gh.calls == [["api", ENV_PATH]]
    assert gh.environments[ENV_PATH] == (None, [])


def test_a_plan_without_branch_policies_keeps_an_unrestricted_environment(tmp_path):
    gh = FakeGh(branch_policies=False)
    assert ensure_environment(gh, "acme/data", "prod", "main", cwd=tmp_path) == "unavailable"
    assert gh.calls == [
        ["api", ENV_PATH],
        ["api", "-X", "PUT", ENV_PATH, *RESTRICT],
        ["api", "-X", "PUT", ENV_PATH],
    ]
    assert ENV_PATH in gh.environments


@pytest.mark.parametrize("status", [404, 422])
def test_a_refused_branch_policy_lifts_the_restriction(tmp_path, status):
    """Custom branch policies without a branch would let no branch deploy at all."""
    gh = FakeGh(branch_policy_status=status)
    assert ensure_environment(gh, "acme/data", "prod", "main", cwd=tmp_path) == "unavailable"
    assert gh.calls[-1] == ["api", "-X", "PUT", ENV_PATH, "-F", "deployment_branch_policy=null"]


def test_a_failed_environment_lookup_exits_5_naming_the_path(tmp_path):
    gh = FakeGh(failing=("api",))
    with pytest.raises(Failure) as caught:
        ensure_environment(gh, "acme/data", "prod", "main", cwd=tmp_path)
    assert caught.value.code == ExitCode.TOOL_FAILED
    assert ENV_PATH in caught.value.message
    assert len(gh.calls) == 1


def test_each_variable_is_set_on_the_environment(tmp_path):
    gh = FakeGh()
    set_environment_variables(
        gh, "acme/data", "prod", {"VECTEN_AWS_REGION": REGION, "VECTEN_STATE_BUCKET": STATE_BUCKET}
    )
    assert gh.calls == [
        [
            "variable",
            "set",
            "VECTEN_AWS_REGION",
            "--env",
            "prod",
            "--body",
            REGION,
            "--repo",
            "acme/data",
        ],
        [
            "variable",
            "set",
            "VECTEN_STATE_BUCKET",
            "--env",
            "prod",
            "--body",
            STATE_BUCKET,
            "--repo",
            "acme/data",
        ],
    ]


def test_repository_variables_carry_no_environment():
    gh = FakeGh()
    set_repository_variables(gh, "acme/data", {"VECTEN_AWS_PLAN_ROLE_ARN": "arn:x"})
    assert gh.calls == [
        ["variable", "set", "VECTEN_AWS_PLAN_ROLE_ARN", "--body", "arn:x", "--repo", "acme/data"]
    ]


def test_a_failed_repository_variable_exits_5():
    with pytest.raises(Failure) as caught:
        set_repository_variables(
            FakeGh(failing=("variable",)), "acme/data", {"VECTEN_AWS_PLAN_ROLE_ARN": "arn:x"}
        )
    assert caught.value.code == ExitCode.TOOL_FAILED
    assert "VECTEN_AWS_PLAN_ROLE_ARN" in caught.value.message


def test_repository_facts_read_the_canonical_name_and_default_branch(tmp_path):
    gh = FakeGh(canonical="Acme/Data")
    assert repository_facts(gh, tmp_path, "acme/data") == ("Acme/Data", "main")
    assert gh.calls == [["repo", "view", "acme/data", "--json", "nameWithOwner,defaultBranchRef"]]


def test_repository_ids_read_the_owner_and_repository_ids(tmp_path):
    gh = FakeGh(canonical="Acme/Data")
    assert repository_ids(gh, tmp_path, "Acme/Data") == (OWNER_ID, REPOSITORY_ID)
    assert gh.calls == [["api", "repos/Acme/Data"]]


def test_oidc_subject_prefixes_name_the_repository_and_its_immutable_ids():
    # Repositories created or renamed after 2026-07-15 send the immutable form; older ones the
    # name-only one until they opt in, so the trust names both.
    assert oidc_subject_prefixes("Acme/Data", 11, 22) == ("repo:Acme/Data", "repo:Acme@11/Data@22")


def test_default_branch_is_read_by_the_same_repo_view(tmp_path):
    gh = FakeGh()
    assert default_branch(gh, tmp_path, "acme/data") == "main"
    assert gh.calls == [["repo", "view", "acme/data", "--json", "nameWithOwner,defaultBranchRef"]]


def test_repository_facts_of_a_missing_repository_name_github_init(tmp_path):
    with pytest.raises(Failure) as caught:
        repository_facts(FakeGh(failing=("repo",)), tmp_path, "acme/data")
    assert caught.value.code == ExitCode.PREREQUISITE
    assert "vecten github init" in caught.value.message


def test_canonical_repository_takes_githubs_casing():
    assert canonical_repository("acme/data", "Acme/Data") == "Acme/Data"
    assert canonical_repository("acme/data", "acme/data") == "acme/data"


def test_canonical_repository_refuses_another_repository():
    with pytest.raises(Failure) as caught:
        canonical_repository("acme/data", "acme/data-old")
    assert caught.value.code == ExitCode.CONFIG
    assert "acme/data-old" in caught.value.message
    assert "'acme/data'" in caught.value.message


def test_a_failed_environment_variable_exits_5_naming_it():
    gh = FakeGh(failing=("variable",))
    with pytest.raises(Failure) as caught:
        set_environment_variables(gh, "acme/data", "prod", {"VECTEN_AWS_REGION": REGION})
    assert caught.value.code == ExitCode.TOOL_FAILED
    assert "VECTEN_AWS_REGION" in caught.value.message
