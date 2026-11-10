"""What `bootstrap` creates in an AWS account with the human's login: the state bucket, the
GitHub OIDC provider, the host roles' permissions boundary, the deploy role CI applies with
and the read-only role it plans with. Every function is safe to run again.

The account's Secrets Manager needs no creation: it exists in every region, and the deploy
role reaches it through PowerUserAccess.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from urllib.parse import unquote

from vecten.deploy.aws.clients import aws_errors, aws_failure, aws_reason, error_code
from vecten.deploy.names import (
    artifacts_bucket_name,
    boundary_policy_name,
    name_prefix,
    plan_role_name,
)
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import secret_prefix

GITHUB_OIDC_URL = "token.actions.githubusercontent.com"
STS_AUDIENCE = "sts.amazonaws.com"
# IAM validates GitHub's OIDC tokens against its own trusted root CAs and ignores the
# thumbprint, but CreateOpenIDConnectProvider still takes a list; these are the values GitHub
# documents for its issuer.
GITHUB_THUMBPRINTS = (
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
)
POWER_USER_ARN = "arn:aws:iam::aws:policy/PowerUserAccess"
INLINE_POLICY = "vecten-deploy-iam"
LOGIN_HINT = "run `aws sso login` (or `aws configure` for access keys), then retry"

SSM_CORE_ARN = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
PLAN_INLINE_POLICY = "vecten-plan-read"
PLAN_READ_ACTIONS = (
    "ec2:Describe*",
    "iam:Get*",
    "iam:List*",
    "rds:Describe*",
    "rds:ListTagsForResource",
    "route53:Get*",
    "route53:List*",
    "sts:GetCallerIdentity",
)
# The AWS module's host keys; it names each host's role and instance profile exactly
# `<name_prefix>-<host key>`, so these names, not a wildcard, are what CI may manage.
HOST_KEYS = ("platform", "mcp", "main")
# IAM keeps at most five versions of a managed policy.
POLICY_VERSION_LIMIT = 5

# PowerUserAccess excludes IAM. Terraform creates each host's role with the environment's
# boundary, its inline scope policy, the SSM core attachment and its instance profile, and
# passes the role to EC2. Every action that could widen a role's permissions is in a
# conditioned statement below, never here.
ROLE_ACTIONS = (
    "iam:DeleteRole",
    "iam:GetRole",
    "iam:GetRolePolicy",
    "iam:ListAttachedRolePolicies",
    "iam:ListInstanceProfilesForRole",
    "iam:ListRolePolicies",
    "iam:ListRoleTags",
    "iam:TagRole",
    "iam:UntagRole",
    "iam:UpdateAssumeRolePolicy",
    "iam:UpdateRole",
    "iam:UpdateRoleDescription",
)
BOUNDED_ROLE_ACTIONS = (
    "iam:CreateRole",
    "iam:DeleteRolePolicy",
    "iam:DetachRolePolicy",
    "iam:PutRolePolicy",
)
INSTANCE_PROFILE_ACTIONS = (
    "iam:AddRoleToInstanceProfile",
    "iam:CreateInstanceProfile",
    "iam:DeleteInstanceProfile",
    "iam:GetInstanceProfile",
    "iam:ListInstanceProfileTags",
    "iam:RemoveRoleFromInstanceProfile",
    "iam:TagInstanceProfile",
    "iam:UntagInstanceProfile",
)
VM_SECRET_SCOPES = ("platform", "mcp")
BOUNDARY_CHANGES = ("iam:DeleteRolePermissionsBoundary", "iam:PutRolePermissionsBoundary")
BOUNDARY_POLICY_CHANGES = (
    "iam:CreatePolicyVersion",
    "iam:DeletePolicy",
    "iam:DeletePolicyVersion",
    "iam:SetDefaultPolicyVersion",
)
# What the SSM agent calls, as AmazonSSMManagedInstanceCore grants it, less the Parameter Store
# reads: nothing on a host reads a parameter, and the boundary caps the attached policy.
SSM_AGENT_ACTIONS = (
    "ec2messages:*",
    "ssm:DescribeAssociation",
    "ssm:DescribeDocument",
    "ssm:DescribeDocumentParameters",
    "ssm:DescribeInstanceProperties",
    "ssm:GetDeployablePatchSnapshotForInstance",
    "ssm:GetDocument",
    "ssm:GetManifest",
    "ssm:ListAssociations",
    "ssm:ListInstanceAssociations",
    "ssm:PutComplianceItems",
    "ssm:PutConfigurePackageResult",
    "ssm:PutInventory",
    "ssm:UpdateAssociationStatus",
    "ssm:UpdateInstanceAssociationStatus",
    "ssm:UpdateInstanceInformation",
    "ssmmessages:*",
)
LOG_ACTIONS = (
    "logs:CreateLogGroup",
    "logs:CreateLogStream",
    "logs:DescribeLogGroups",
    "logs:DescribeLogStreams",
    "logs:PutLogEvents",
)
# CI never needs to change its own role or the plan role; bootstrap does that with the
# human's login.
OWN_ROLE_DENIED = (
    "iam:AttachRolePolicy",
    "iam:DeleteRole",
    "iam:DeleteRolePermissionsBoundary",
    "iam:DeleteRolePolicy",
    "iam:DetachRolePolicy",
    "iam:PassRole",
    "iam:PutRolePermissionsBoundary",
    "iam:PutRolePolicy",
    "iam:TagRole",
    "iam:UntagRole",
    "iam:UpdateAssumeRolePolicy",
    "iam:UpdateRole",
)
_LOGIN_CODES = frozenset(
    {
        "ExpiredToken",
        "ExpiredTokenException",
        "InvalidClientTokenId",
        "RequestExpired",
        "SignatureDoesNotMatch",
        "UnrecognizedClientException",
    }
)
_MISSING_BUCKET = frozenset({"404", "NoSuchBucket", "NotFound"})


def _login_errors() -> tuple[type[Exception], ...]:
    from botocore.exceptions import (
        CredentialRetrievalError,
        NoCredentialsError,
        PartialCredentialsError,
        ProfileNotFound,
        SSOError,
        TokenRetrievalError,
    )

    return (
        CredentialRetrievalError,
        NoCredentialsError,
        PartialCredentialsError,
        ProfileNotFound,
        SSOError,
        TokenRetrievalError,
    )


def login_failure(exc: Exception) -> Failure | None:
    """The refusal for an exception that means the human holds no usable AWS login."""
    if isinstance(exc, _login_errors()) or error_code(exc) in _LOGIN_CODES:
        return Failure(
            ExitCode.PREREQUISITE, f"no usable AWS login ({aws_reason(exc)}); {LOGIN_HINT}"
        )
    return None


def caller_account(sts: Any) -> str:
    """The 12-digit account the human's login acts in; refuses without a usable login."""
    try:
        return str(sts.get_caller_identity()["Account"])
    except aws_errors() as exc:
        raise (login_failure(exc) or aws_failure("STS GetCallerIdentity", exc)) from None


def _call(operation: str, method: Any, **kwargs: Any) -> Any:
    try:
        return method(**kwargs)
    except aws_errors() as exc:
        raise aws_failure(operation, exc) from None


def _bucket_exists(s3: Any, name: str) -> bool:
    try:
        s3.head_bucket(Bucket=name)
    except aws_errors() as exc:
        if error_code(exc) in _MISSING_BUCKET:
            return False
        raise aws_failure("S3 HeadBucket", exc) from None
    return True


def _secure_transport_policy(name: str) -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "DenyInsecureTransport",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "s3:*",
                    "Resource": [f"arn:aws:s3:::{name}", f"arn:aws:s3:::{name}/*"],
                    "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                }
            ],
        }
    )


def ensure_state_bucket(s3: Any, name: str, region: str) -> bool:
    """Create the bucket when absent, then make it versioned, encrypted, private and TLS-only.

    Returns whether the bucket was created. The settings are applied on every run, so a bucket
    someone loosened by hand is tightened again.
    """
    created = False
    if not _bucket_exists(s3, name):
        kwargs: dict[str, Any] = {"Bucket": name}
        # us-east-1 is S3's default location and CreateBucket refuses it as a constraint.
        if region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        try:
            s3.create_bucket(**kwargs)
            created = True
        except aws_errors() as exc:
            if error_code(exc) != "BucketAlreadyOwnedByYou":
                raise aws_failure("S3 CreateBucket", exc) from None
    _call(
        "S3 PutPublicAccessBlock",
        s3.put_public_access_block,
        Bucket=name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    _call(
        "S3 PutBucketVersioning",
        s3.put_bucket_versioning,
        Bucket=name,
        VersioningConfiguration={"Status": "Enabled"},
    )
    _call(
        "S3 PutBucketEncryption",
        s3.put_bucket_encryption,
        Bucket=name,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )
    _call(
        "S3 PutBucketPolicy",
        s3.put_bucket_policy,
        Bucket=name,
        Policy=_secure_transport_policy(name),
    )
    return created


def ensure_oidc_provider(iam: Any, *, created: list[str] | None = None) -> str:
    """The account's GitHub Actions OIDC provider, created when absent.

    An account holds one provider per issuer URL, so a provider another project created is
    reused and only gains the STS audience when it lacks it.
    """
    listed = _call("IAM ListOpenIDConnectProviders", iam.list_open_id_connect_providers)
    for entry in listed.get("OpenIDConnectProviderList", []):
        arn = entry["Arn"]
        if not arn.endswith(f":oidc-provider/{GITHUB_OIDC_URL}"):
            continue
        found = _call(
            "IAM GetOpenIDConnectProvider",
            iam.get_open_id_connect_provider,
            OpenIDConnectProviderArn=arn,
        )
        if STS_AUDIENCE not in found.get("ClientIDList", []):
            _call(
                "IAM AddClientIDToOpenIDConnectProvider",
                iam.add_client_id_to_open_id_connect_provider,
                OpenIDConnectProviderArn=arn,
                ClientID=STS_AUDIENCE,
            )
        return arn
    response = _call(
        "IAM CreateOpenIDConnectProvider",
        iam.create_open_id_connect_provider,
        Url=f"https://{GITHUB_OIDC_URL}",
        ClientIDList=[STS_AUDIENCE],
        ThumbprintList=list(GITHUB_THUMBPRINTS),
    )
    if created is not None:
        created.append("oidc_provider")
    return response["OpenIDConnectProviderArn"]


def _account_of(arn: str) -> str:
    return arn.split(":")[4]


def trust_policy(oidc_arn: str, subject_prefixes: Sequence[str], env: str) -> dict:
    """Only workflow jobs that declare `environment: <env>` in the repository the subject
    prefixes name may assume the role."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Federated": oidc_arn},
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringEquals": {
                        f"{GITHUB_OIDC_URL}:aud": STS_AUDIENCE,
                        f"{GITHUB_OIDC_URL}:sub": [
                            f"{prefix}:environment:{env}" for prefix in subject_prefixes
                        ],
                    }
                },
            }
        ],
    }


def boundary_arn(account: str, project: str, env: str) -> str:
    return f"arn:aws:iam::{account}:policy/{boundary_policy_name(project, env)}"


def _role_arn(account: str, name: str) -> str:
    return f"arn:aws:iam::{account}:role/{name}"


def iam_policy(account: str, project: str, env: str, role_name: str) -> dict:
    """IAM for CI, confined to the exact role and instance-profile names the AWS module gives
    this environment's hosts, so no other environment's names match.

    A role CI creates or edits must carry the environment's boundary, which CI can neither
    remove nor rewrite, so no role it touches can reach beyond what the hosts need.

    PassRole carries no `iam:PassedToService` condition: AddRoleToInstanceProfile checks
    PassRole without that key, so the condition would refuse the instance profiles during
    apply. The roles it names all carry the boundary, which caps whatever they are passed to.
    """
    prefix = name_prefix(project, env)
    boundary = boundary_arn(account, project, env)
    roles = [_role_arn(account, f"{prefix}-{host}") for host in HOST_KEYS]
    profiles = [f"arn:aws:iam::{account}:instance-profile/{prefix}-{host}" for host in HOST_KEYS]
    own = [_role_arn(account, role_name), _role_arn(account, plan_role_name(project, env))]
    with_boundary = {"iam:PermissionsBoundary": boundary}
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ManageHostRoles",
                "Effect": "Allow",
                "Action": list(ROLE_ACTIONS),
                "Resource": roles,
            },
            {
                "Sid": "BoundedRoleWrites",
                "Effect": "Allow",
                "Action": list(BOUNDED_ROLE_ACTIONS),
                "Resource": roles,
                "Condition": {"StringEquals": with_boundary},
            },
            {
                # The module attaches no managed policy but the SSM core one.
                "Sid": "AttachSsmCore",
                "Effect": "Allow",
                "Action": "iam:AttachRolePolicy",
                "Resource": roles,
                "Condition": {
                    "StringEquals": with_boundary,
                    "ArnEquals": {"iam:PolicyARN": SSM_CORE_ARN},
                },
            },
            {
                # No `iam:PassedToService`: AddRoleToInstanceProfile's PassRole check lacks it.
                "Sid": "PassHostRoles",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": roles,
            },
            {
                "Sid": "ManageHostInstanceProfiles",
                "Effect": "Allow",
                "Action": list(INSTANCE_PROFILE_ACTIONS),
                "Resource": profiles,
            },
            {
                "Sid": "KeepRoleBoundaries",
                "Effect": "Deny",
                "Action": list(BOUNDARY_CHANGES),
                "Resource": roles + own,
            },
            {
                "Sid": "ProtectBoundaryPolicy",
                "Effect": "Deny",
                "Action": list(BOUNDARY_POLICY_CHANGES),
                # Every environment's boundary, so a neighbour whose prefix extends this one's
                # cannot have its ceiling rewritten either; bootstrap edits boundaries, not CI.
                "Resource": [boundary, f"arn:aws:iam::{account}:policy/vecten-*-boundary"],
            },
            {
                "Sid": "NotItsOwnRole",
                "Effect": "Deny",
                "Action": list(OWN_ROLE_DENIED),
                "Resource": own,
            },
        ],
    }


def boundary_policy(account: str, project: str, env: str) -> dict:
    """The ceiling of every host role: the environment's platform and mcp secrets, the RDS
    master secrets, the artifacts bucket, the SSM agent and CloudWatch Logs, and no IAM at all.

    `deploy/` and `admin/` stay outside it: no VM reads them. The trailing `*` covers both a
    secret's bare name and its `-??????` ARN suffix.
    """
    artifacts = f"arn:aws:s3:::{artifacts_bucket_name(project, env, account)}"
    secrets = f"arn:aws:secretsmanager:*:{account}:secret:"
    prefix = secret_prefix(project, env)
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "EnvironmentSecrets",
                "Effect": "Allow",
                "Action": [
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:CreateSecret",
                    "secretsmanager:PutSecretValue",
                ],
                "Resource": [f"{secrets}{prefix}{scope}/*" for scope in VM_SECRET_SCOPES],
            },
            {
                # Secrets Manager encrypts with the AWS-managed key, and a host reading or
                # writing a secret uses that key through the service.
                "Sid": "SecretsManagerKeys",
                "Effect": "Allow",
                "Action": ["kms:Decrypt", "kms:GenerateDataKey"],
                "Resource": "*",
                "Condition": {"StringLike": {"kms:ViaService": "secretsmanager.*.amazonaws.com"}},
            },
            {
                # RDS names the master secrets it manages `rds!<id>`; the setup host's own
                # policy names the exact ARNs, this only keeps them under the ceiling.
                "Sid": "RdsMasterSecrets",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                "Resource": f"{secrets}rds!*",
            },
            {
                "Sid": "ArtifactObjects",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": f"{artifacts}/*",
            },
            {
                "Sid": "ArtifactBucket",
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": artifacts,
            },
            {
                "Sid": "SsmAgent",
                "Effect": "Allow",
                "Action": list(SSM_AGENT_ACTIONS),
                "Resource": "*",
            },
            {
                "Sid": "Logs",
                "Effect": "Allow",
                "Action": list(LOG_ACTIONS),
                "Resource": f"arn:aws:logs:*:{account}:log-group:*",
            },
        ],
    }


def _document(raw: Any) -> Any:
    # IAM returns policy documents URL-encoded; boto3 decodes them, other clients may not.
    return json.loads(unquote(raw)) if isinstance(raw, str) else raw


def _prune_versions(iam: Any, arn: str) -> None:
    versions = _call("IAM ListPolicyVersions", iam.list_policy_versions, PolicyArn=arn)["Versions"]
    if len(versions) < POLICY_VERSION_LIMIT:
        return
    # IAM numbers versions v1, v2, ... in creation order and never reuses a number.
    oldest = min(
        (v for v in versions if not v.get("IsDefaultVersion")),
        key=lambda v: int(str(v["VersionId"]).lstrip("v")),
    )
    _call(
        "IAM DeletePolicyVersion",
        iam.delete_policy_version,
        PolicyArn=arn,
        VersionId=oldest["VersionId"],
    )


def ensure_boundary_policy(
    iam: Any, account: str, project: str, env: str, *, created: list[str] | None = None
) -> str:
    """The environment's permissions boundary; a changed document becomes the new default
    version, dropping the oldest non-default version when IAM's limit of five is reached."""
    arn = boundary_arn(account, project, env)
    wanted = boundary_policy(account, project, env)
    try:
        current = iam.get_policy(PolicyArn=arn)["Policy"]
    except aws_errors() as exc:
        if error_code(exc) != "NoSuchEntity":
            raise aws_failure("IAM GetPolicy", exc) from None
        _call(
            "IAM CreatePolicy",
            iam.create_policy,
            PolicyName=boundary_policy_name(project, env),
            PolicyDocument=json.dumps(wanted),
            Description=f"vecten permissions boundary of {project} environment {env}'s hosts",
        )
        if created is not None:
            created.append("boundary_policy")
        return arn
    version = _call(
        "IAM GetPolicyVersion",
        iam.get_policy_version,
        PolicyArn=arn,
        VersionId=current["DefaultVersionId"],
    )
    if _document(version["PolicyVersion"]["Document"]) == wanted:
        return arn
    _prune_versions(iam, arn)
    _call(
        "IAM CreatePolicyVersion",
        iam.create_policy_version,
        PolicyArn=arn,
        PolicyDocument=json.dumps(wanted),
        SetAsDefault=True,
    )
    return arn


def _role(
    iam: Any, name: str, trust: dict, description: str, created: list[str] | None, kind: str
) -> str:
    """Create the role, or give an existing one the current trust while it keeps its id."""
    document = json.dumps(trust)
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    except aws_errors() as exc:
        if error_code(exc) != "NoSuchEntity":
            raise aws_failure("IAM GetRole", exc) from None
    else:
        _call(
            "IAM UpdateAssumeRolePolicy",
            iam.update_assume_role_policy,
            RoleName=name,
            PolicyDocument=document,
        )
        return arn
    arn = _call(
        "IAM CreateRole",
        iam.create_role,
        RoleName=name,
        AssumeRolePolicyDocument=document,
        Description=description,
    )["Role"]["Arn"]
    if created is not None:
        created.append(kind)
    return arn


def ensure_deploy_role(
    iam: Any,
    name: str,
    oidc_arn: str,
    repository: str,
    env: str,
    project: str,
    *,
    subject_prefixes: Sequence[str],
    created: list[str] | None = None,
) -> str:
    """The role CI assumes over OIDC; an existing role keeps its id and gets the current trust."""
    description = f"vecten deploy role for {repository} environment {env}"
    trust = trust_policy(oidc_arn, subject_prefixes, env)
    arn = _role(iam, name, trust, description, created, "deploy_role")
    _call("IAM AttachRolePolicy", iam.attach_role_policy, RoleName=name, PolicyArn=POWER_USER_ARN)
    _call(
        "IAM PutRolePolicy",
        iam.put_role_policy,
        RoleName=name,
        PolicyName=INLINE_POLICY,
        PolicyDocument=json.dumps(iam_policy(_account_of(oidc_arn), project, env, name)),
    )
    return arn


def plan_trust_policy(oidc_arn: str, subject_prefixes: Sequence[str], branch: str) -> dict:
    """Pull-request jobs and default-branch pushes in the repository the subject prefixes
    name, which declare no environment, may assume the plan role."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Federated": oidc_arn},
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringEquals": {
                        f"{GITHUB_OIDC_URL}:aud": STS_AUDIENCE,
                        f"{GITHUB_OIDC_URL}:sub": [
                            subject
                            for prefix in subject_prefixes
                            for subject in (
                                f"{prefix}:pull_request",
                                f"{prefix}:ref:refs/heads/{branch}",
                            )
                        ],
                    }
                },
            }
        ],
    }


def plan_policy(account: str, project: str, env: str, state_bucket: str) -> dict:
    """What a plan of the AWS module reads, and the only writes it makes: Terraform's lock
    files in the state bucket.

    The reads cover the refresh of every resource and data source type the module declares:
    VPC, subnets, route tables, gateways, security groups and their rules, network interfaces,
    Elastic IPs, instances and availability zones (`ec2:Describe*`); RDS instances, parameter
    and subnet groups (`rds:Describe*`, `rds:ListTagsForResource`); host roles, their policies
    and instance profiles (`iam:Get*`, `iam:List*`); the hosted zone and records
    (`route53:Get*`, `route53:List*`); the caller identity; the public AMI parameter; and the
    artifacts bucket's configuration. No other bucket, and no secret, is readable.
    """
    bucket = f"arn:aws:s3:::{state_bucket}"
    artifacts = f"arn:aws:s3:::{artifacts_bucket_name(project, env, account)}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadInfrastructure",
                "Effect": "Allow",
                "Action": list(PLAN_READ_ACTIONS),
                "Resource": "*",
            },
            {
                "Sid": "ReadAmiParameters",
                "Effect": "Allow",
                "Action": ["ssm:GetParameter", "ssm:GetParameters"],
                "Resource": "arn:aws:ssm:*::parameter/aws/service/*",
            },
            {
                # The bucket's configuration only: its objects hold task logs, and a plan
                # refreshes no object.
                "Sid": "ReadArtifactsBucket",
                "Effect": "Allow",
                "Action": ["s3:Get*", "s3:List*"],
                "Resource": artifacts,
            },
            {
                "Sid": "ReadState",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": [bucket, f"{bucket}/*"],
            },
            {
                "Sid": "StateLocks",
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:DeleteObject"],
                "Resource": f"{bucket}/{env}/*.tflock",
            },
        ],
    }


def ensure_plan_role(
    iam: Any,
    name: str,
    oidc_arn: str,
    repository: str,
    branch: str,
    state_bucket: str,
    env: str,
    project: str,
    *,
    subject_prefixes: Sequence[str],
    created: list[str] | None = None,
) -> str:
    """The read-only role the plan workflow assumes; an existing role gets the current trust.

    Its one inline policy is all it holds: any managed policy found attached is detached, so
    a pull-request job never reads beyond what a plan refreshes.
    """
    description = f"vecten plan role for {repository} environment {env}"
    trust = plan_trust_policy(oidc_arn, subject_prefixes, branch)
    arn = _role(iam, name, trust, description, created, "plan_role")
    attached = _call("IAM ListAttachedRolePolicies", iam.list_attached_role_policies, RoleName=name)
    for policy in attached.get("AttachedPolicies", []):
        _call(
            "IAM DetachRolePolicy",
            iam.detach_role_policy,
            RoleName=name,
            PolicyArn=policy["PolicyArn"],
        )
    _call(
        "IAM PutRolePolicy",
        iam.put_role_policy,
        RoleName=name,
        PolicyName=PLAN_INLINE_POLICY,
        PolicyDocument=json.dumps(plan_policy(_account_of(oidc_arn), project, env, state_bucket)),
    )
    return arn
