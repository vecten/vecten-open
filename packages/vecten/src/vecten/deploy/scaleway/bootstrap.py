"""What `bootstrap` creates in a Scaleway organization with the human's credentials: the
resources and state projects, the state bucket and its policy, the IAM applications CI deploys
and plans with and one per host, their policies, and their keys. Every step is safe to rerun.

CI holds no IAM permission set, so every application, policy and key is made here, and a host
that left the configuration loses its application here too. Keys expire after a year; a rerun
within 30 days of expiry rotates them, and each key's expiry is recorded in the facts as
`cloud_ids["expires_<owner>"]` so `next` and `deploy` warn in time. A replaced key stays
valid until a later run: a CI key because a workflow run already under way still signs with
it, a host's key until the host has acknowledged the new one, because its running containers
still sign with the old key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from vecten.deploy.expiry import RENEW_WITHIN
from vecten.deploy.names import name_prefix, scaleway_bucket_name, scaleway_project_name
from vecten.deploy.scaleway import clients
from vecten.deploy.scaleway.clients import LOGIN_HINT, ApiError, Credentials
from vecten.deploy.vm.apply import AIRFLOW_APP_SECRETS
from vecten_shared.config import CLOUD_ID_EXPIRY_PREFIX
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import scope_for
from vecten_shared.provider.scaleway import scaleway_resource_name, scaleway_secret_path

KEY_LIFETIME = timedelta(days=365)
ROTATE_WITHIN = RENEW_WITHIN
PAGE_SIZE = 100
# Every host key the Scaleway module can declare; an application named for one of them that
# the current configuration lacks is deleted.
HOST_KEYS = ("platform", "mcp", "main")
COMPONENT_ORDER = ("platform", "mcp")
# VM keys live in the deploy scope, which only the deploy application reads.
HOST_KEY_SCOPE = "deploy"
STATE_NONCURRENT_DAYS = 90
POLICY_VERSION = "2023-04-17"

DEPLOY_RESOURCE_SETS = (
    "InstancesFullAccess",
    "PrivateNetworksFullAccess",
    "IPAMReadOnly",
    "RelationalDatabasesFullAccess",
    "SecretManagerFullAccess",
    "ObjectStorageFullAccess",
    "ObjectStorageBucketPolicyFullAccess",
    # POP2 and PRO2 servers boot from an `sbs_volume`, which the provider manages through the
    # Block Storage API rather than the Instances API.
    "BlockStorageFullAccess",
)
STATE_OBJECT_SETS = (
    "ObjectStorageObjectsRead",
    "ObjectStorageObjectsWrite",
    "ObjectStorageObjectsDelete",
    "ObjectStorageBucketsRead",
)
# Pull-request plans on Scaleway run without a refresh and the root module declares no data
# source, so the plan key reads nothing in the resources project: no set there, and none
# that could read a VM key from instance user data. It reads state and writes lock files.
PLAN_STATE_SETS = (
    "ObjectStorageObjectsRead",
    "ObjectStorageObjectsWrite",
    "ObjectStorageObjectsDelete",
)
HOST_OBJECT_SETS = ("ObjectStorageObjectsRead", "ObjectStorageObjectsWrite")
# The error types CreateAPIKey answers with when an `expires_at` breaks the organization's
# credentials maximum duration; HTTP 400 counts too, since the request carries no other value
# Scaleway could reject.
DURATION_REFUSALS = ("invalid_arguments", "precondition_failed")

ENV_ACCESS_SECRET = "VECTEN_SCW_ACCESS_KEY"
ENV_SECRET_SECRET = "VECTEN_SCW_SECRET_KEY"
PLAN_ACCESS_PREFIX = "VECTEN_SCW_PLAN_ACCESS_KEY_"
PLAN_SECRET_PREFIX = "VECTEN_SCW_PLAN_SECRET_KEY_"
PROJECT_VARIABLE = "VECTEN_SCW_PROJECT_ID"
STATE_PROJECT_VARIABLE = "VECTEN_SCW_STATE_PROJECT_ID"
REGION_VARIABLE = "VECTEN_SCW_REGION"
STATE_BUCKET_VARIABLE = "VECTEN_STATE_BUCKET"


def now() -> datetime:
    return datetime.now(UTC)


def _ts(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _github_suffix(env: str) -> str:
    # GitHub secret names hold letters, digits and underscores; environment names add hyphens.
    return env.upper().replace("-", "_")


def plan_secret_names(env: str) -> tuple[str, str]:
    """The repository-level secrets holding `env`'s plan key, one pair per environment."""
    suffix = _github_suffix(env)
    return f"{PLAN_ACCESS_PREFIX}{suffix}", f"{PLAN_SECRET_PREFIX}{suffix}"


def host_key_name(host: str) -> str:
    return f"HOST_KEY_{host.upper()}"


@dataclass(frozen=True)
class Layout:
    """The hosts and secrets the Scaleway module declares for one tfvars file."""

    components: tuple[str, ...]
    profile: str
    warehouse: str

    @property
    def ordered(self) -> list[str]:
        return [c for c in COMPONENT_ORDER if c in self.components]

    @property
    def hosts(self) -> dict[str, list[str]]:
        if self.profile == "minimal":
            return {"main": self.ordered}
        return {c: [c] for c in self.ordered}

    @property
    def setup_host(self) -> str:
        if self.profile == "minimal":
            return "main"
        return "mcp" if "mcp" in self.components else "platform"

    def generated_names(self) -> list[str]:
        """The credentials the setup host creates; mirrors the module's `generated_secrets`."""
        managed = "platform" in self.components and self.warehouse == "postgres"
        names = []
        if "platform" in self.components:
            names.append("AIRFLOW_DB_PASSWORD")
        if managed:
            names.append("LOADER_PASSWORD")
        if "mcp" in self.components:
            names.append("MCP_STATE_DB_PASSWORD")
            if managed:
                names += ["MCP_PROVISIONER_PASSWORD", "MCP_IMPERSONATOR_PASSWORD"]
        return names


def _rule(sets: tuple[str, ...] | list[str], project_id: str, condition: str = "") -> dict:
    rule: dict[str, Any] = {"permission_set_names": list(sets), "project_ids": [project_id]}
    if condition:
        rule["condition"] = condition
    return rule


def _under(path: str) -> str:
    return f'resource.name.startsWith("{path}/")'


def _named(resource_name: str) -> str:
    return f'resource.name == "{resource_name}"'


def host_rules(layout: Layout, project: str, env: str, project_id: str, host: str) -> list[dict]:
    """The rules of host `host`'s policy, every one on the resources project only.

    Secret access names whole scope paths with `startsWith` and single secrets with `==`.
    Listing has no single secret to evaluate, so the listing rule alone adds
    `!has(resource.id)`; it grants names and metadata, never a value.
    """
    components = layout.hosts[host]
    setup = host == layout.setup_host
    scopes = [*components, *(["setup"] if setup else [])]
    readable = [_under(scaleway_secret_path(project, env, s)) for s in scopes]
    writable: list[str] = []
    if setup:
        generated = layout.generated_names()
        readable += [
            _named(scaleway_resource_name(project, env, n))
            for n in generated
            if scope_for(n) not in scopes
        ]
        writable += generated
    if "platform" in components:
        writable += AIRFLOW_APP_SECRETS
    if "mcp" in components:
        writable += ["MCP_IMPERSONATOR_PASSWORD", "MCP_SIGNING_KEY"]
    access = " || ".join(readable)
    rules = [
        _rule(["SecretManagerSecretAccess"], project_id, access),
        _rule(["SecretManagerReadOnly"], project_id, f"{access} || !has(resource.id)"),
    ]
    if writable:
        names = list(dict.fromkeys(writable))
        create = " || ".join(_named(scaleway_resource_name(project, env, n)) for n in names)
        rules.append(_rule(["SecretManagerSecretCreate"], project_id, create))
    rules.append(_rule(HOST_OBJECT_SETS, project_id))
    return rules


def _with_dns(sets: tuple[str, ...], dns_set: str, resources_id: str, dns_id: str) -> list[dict]:
    if dns_id == resources_id:
        return [_rule([*sets, dns_set], resources_id)]
    return [_rule(sets, resources_id), _rule([dns_set], dns_id)]


def deploy_rules(resources_id: str, state_id: str, dns_id: str) -> list[dict]:
    """What the deploy key applies, ships and snapshots with; no IAM set, so CI can never
    widen a key, its own included."""
    return [
        *_with_dns(DEPLOY_RESOURCE_SETS, "DomainsDNSFullAccess", resources_id, dns_id),
        _rule(STATE_OBJECT_SETS, state_id),
    ]


def plan_rules(state_id: str) -> list[dict]:
    """What a pull request's plan runs with: state reads, and object writes the state
    bucket's policy cuts to `<env>/*.tflock`."""
    return [_rule(PLAN_STATE_SETS, state_id)]


def state_bucket_policy(
    bucket: str, env: str, *, deploy_app: str, plan_app: str, operator: str
) -> dict:
    """The state bucket's policy. Under version 2023-04-17 whatever no statement allows is
    denied, so the operator who ran bootstrap is named too: without it the next run could not
    reach the bucket's settings."""
    everything = [bucket, f"{bucket}/*"]
    deploy = {"SCW": f"application_id:{deploy_app}"}
    plan = {"SCW": f"application_id:{plan_app}"}
    return {
        "Version": POLICY_VERSION,
        "Id": "vecten-state",
        "Statement": [
            {
                "Sid": "OperatorFullAccess",
                "Effect": "Allow",
                "Principal": {"SCW": operator},
                "Action": ["s3:*"],
                "Resource": everything,
            },
            {
                "Sid": "DeployObjects",
                "Effect": "Allow",
                "Principal": deploy,
                "Action": ["s3:ListBucket", "s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                "Resource": everything,
            },
            {
                "Sid": "PlanReadsState",
                "Effect": "Allow",
                "Principal": plan,
                "Action": ["s3:ListBucket", "s3:GetObject"],
                "Resource": everything,
            },
            {
                "Sid": "PlanWritesLocks",
                "Effect": "Allow",
                "Principal": plan,
                "Action": ["s3:PutObject", "s3:DeleteObject"],
                "Resource": [f"{bucket}/{env}/*.tflock"],
            },
        ],
    }


# --- REST helpers ------------------------------------------------------------------------


def _paged(api: Any, path: str, key: str, params: dict[str, Any]) -> list[dict]:
    found: list[dict] = []
    page = 1
    while True:
        doc = api.request("GET", path, params={**params, "page": page, "page_size": PAGE_SIZE})
        batch = [item for item in doc.get(key) or [] if isinstance(item, dict)]
        found.extend(batch)
        total = doc.get("total_count")
        if not batch or not isinstance(total, int) or len(found) >= total:
            return found
        page += 1


def operator_principal(iam: Any, creds: Credentials) -> str:
    """The bucket-policy principal of the human's key; refuses a key Scaleway rejects."""
    try:
        key = iam.request("GET", f"/api-keys/{quote(creds.access_key, safe='')}")
    except ApiError as error:
        if error.status in (401, 404):
            raise Failure(
                ExitCode.PREREQUISITE,
                f"Scaleway rejected the API key in your login (HTTP {error.status}); {LOGIN_HINT}",
            ) from None
        if error.status == 403:
            raise Failure(
                ExitCode.PREREQUISITE,
                "your Scaleway login may not read its own API key in IAM (HTTP 403), which "
                "vecten checks before every command that uses it: grant IAMReadOnly in the "
                "organization; bootstrap also creates applications, policies and keys, which "
                "needs IAMManager",
            ) from None
        raise
    if key.get("user_id"):
        return f"user_id:{key['user_id']}"
    if key.get("application_id"):
        return f"application_id:{key['application_id']}"
    raise Failure(ExitCode.TOOL_FAILED, "Scaleway named no user or application for your API key")


# cloud_ids values are plain identifiers, so the principal's kind travels in the key.
OPERATOR_KEYS = {"user_id": "operator_user", "application_id": "operator_application"}


def operator_ids(principal: str) -> dict[str, str]:
    kind, _, ident = principal.partition(":")
    return {OPERATOR_KEYS[kind]: ident}


def operator_principal_of(cloud_ids: dict[str, str]) -> str | None:
    """The bucket-policy principal bootstrap recorded, or None for an older bootstrap."""
    for kind, key in OPERATOR_KEYS.items():
        if cloud_ids.get(key):
            return f"{kind}:{cloud_ids[key]}"
    return None


def dns_zone_name(zone: dict[str, Any]) -> str:
    subdomain, domain = str(zone.get("subdomain") or ""), str(zone.get("domain") or "")
    return f"{subdomain}.{domain}" if subdomain else domain


def find_dns_project(domain_api: Any, organization: str, dns_zone: str) -> str:
    """The project holding `dns_zone` in the organization; refuses when no project does.

    `GET /domain/v2beta1/dns-zones` with `organization_id` and `dns_zones` answers
    `{dns_zones: [{domain, subdomain, project_id, ...}], total_count}` (Scaleway Go SDK,
    `api/domain/v2beta1/domain_sdk.go`, `ListDNSZones`). A zone's full name is its subdomain,
    a dot and its domain, or the domain alone, and only an exact match counts.
    """
    wanted = dns_zone.strip().rstrip(".").lower()
    zones = _paged(
        domain_api,
        "/dns-zones",
        "dns_zones",
        {"organization_id": organization, "dns_zones": [wanted]},
    )
    for zone in zones:
        if dns_zone_name(zone).lower() == wanted and zone.get("project_id"):
            return str(zone["project_id"])
    raise Failure(
        ExitCode.PREREQUISITE,
        f"DNS zone {dns_zone} is not in Scaleway organization {organization}: it must exist "
        "there, in Domains and DNS in any of its projects, before bootstrap; create or delegate "
        "it, then run bootstrap again",
    )


def project_marker(role: str, project: str, env: str) -> str:
    """The description that claims a Scaleway project for one role of one environment."""
    return f"vecten:{role}:{project}:{env}"


def find_project(account: Any, organization: str, name: str, marker: str) -> str | None:
    """The project named `name`, refusing one that carries another marker.

    Names alone can collide: project `acme-prod` with environment `state` names its resources
    project exactly as project `acme` names the state project of `prod`.
    """
    found = _paged(
        account, "/projects", "projects", {"organization_id": organization, "name": name}
    )
    for project in found:
        if project.get("name") == name and project.get("id"):
            description = project.get("description") or ""
            if description != marker:
                raise Failure(
                    ExitCode.REFUSED,
                    f"Scaleway project {name} ({project['id']}) is marked "
                    f"{description!r}, not {marker!r}; it belongs to another environment or "
                    "was not made by vecten, so bootstrap leaves it alone",
                )
            return str(project["id"])
    return None


def create_project(account: Any, organization: str, name: str, marker: str) -> str:
    created = account.request(
        "POST",
        "/projects",
        json={"name": name, "organization_id": organization, "description": marker},
    )
    return str(created["id"])


def find_application(iam: Any, organization: str, name: str) -> str | None:
    found = _paged(
        iam, "/applications", "applications", {"organization_id": organization, "name": name}
    )
    for app in found:
        if app.get("name") == name and app.get("id"):
            return str(app["id"])
    return None


def ensure_application(
    iam: Any, organization: str, name: str, description: str
) -> tuple[str, bool]:
    existing = find_application(iam, organization, name)
    if existing:
        return existing, False
    created = iam.request(
        "POST",
        "/applications",
        json={"name": name, "organization_id": organization, "description": description},
    )
    return str(created["id"]), True


def _policy_of(iam: Any, organization: str, app_id: str, name: str) -> str | None:
    found = _paged(
        iam,
        "/policies",
        "policies",
        {"organization_id": organization, "application_ids": [app_id], "policy_name": name},
    )
    for policy in found:
        if policy.get("name") == name and policy.get("application_id") == app_id:
            return str(policy["id"])
    return None


def _rule_shape(rule: dict) -> tuple:
    return (
        tuple(sorted(rule.get("permission_set_names") or [])),
        tuple(sorted(rule.get("project_ids") or [])),
        rule.get("organization_id") or "",
        rule.get("condition") or "",
    )


def ensure_policy(
    iam: Any, organization: str, app_id: str, name: str, rules: list[dict]
) -> str | None:
    """`created`, `updated` when the rules differed and were replaced, else None."""
    policy_id = _policy_of(iam, organization, app_id, name)
    if policy_id is None:
        iam.request(
            "POST",
            "/policies",
            json={
                "name": name,
                "organization_id": organization,
                "application_id": app_id,
                "description": "Managed by vecten bootstrap; a rerun rewrites its rules.",
                "rules": rules,
            },
        )
        return "created"
    current = _paged(iam, "/rules", "rules", {"policy_id": policy_id})
    if sorted(map(_rule_shape, current)) == sorted(map(_rule_shape, rules)):
        return None
    iam.request("PUT", "/rules", json={"policy_id": policy_id, "rules": rules})
    return "updated"


def list_keys(iam: Any, organization: str, app_id: str) -> list[dict]:
    # IAM refuses a key listing that names no organization, whatever the bearer filter.
    return _paged(
        iam,
        "/api-keys",
        "api_keys",
        {"organization_id": organization, "bearer_type": "application", "bearer_id": app_id},
    )


def create_key(
    iam: Any, app_id: str, project_id: str, description: str, moment: datetime
) -> tuple[str, str]:
    """A key of `app_id` that expires `KEY_LIFETIME` from `moment`, as (access, secret)."""
    try:
        created = iam.request(
            "POST",
            "/api-keys",
            json={
                "application_id": app_id,
                "default_project_id": project_id,
                "description": description,
                "expires_at": _ts(moment + KEY_LIFETIME),
            },
        )
    except ApiError as error:
        if error.status == 400 or error.kind in DURATION_REFUSALS:
            raise _duration_failure(error) from None
        raise
    access, secret = created.get("access_key"), created.get("secret_key")
    if not access or not secret:
        raise Failure(ExitCode.TOOL_FAILED, "Scaleway CreateAPIKey answered without the key")
    return str(access), str(secret)


def _duration_failure(error: ApiError) -> Failure:
    reason = f"HTTP {error.status}" + (f" {error.kind}" if error.kind else "")
    return Failure(
        ExitCode.PREREQUISITE,
        f"Scaleway refused an API key that expires in {KEY_LIFETIME.days} days ({reason}). "
        "An organization whose credentials maximum duration setting is shorter refuses such "
        f"keys: allow at least {KEY_LIFETIME.days} days under Organization settings, Security, "
        "Credentials maximum duration, then run bootstrap again",
    )


def delete_key(iam: Any, access: str) -> None:
    try:
        iam.request("DELETE", f"/api-keys/{quote(access, safe='')}")
    except ApiError as error:
        if error.status != 404:
            raise


def _fresh(key: dict, moment: datetime) -> bool:
    expires = _time(key.get("expires_at"))
    return expires is not None and expires - moment > ROTATE_WITHIN


def find_secret(secrets: Any, project_id: str, path: str, name: str) -> str | None:
    found = _paged(
        secrets, "/secrets", "secrets", {"project_id": project_id, "path": path, "name": name}
    )
    for secret in found:
        if secret.get("path") == path and secret.get("name") == name and secret.get("id"):
            return str(secret["id"])
    return None


def read_latest(secrets: Any, secret_id: str) -> str | None:
    try:
        doc = secrets.request(
            "GET", f"/secrets/{quote(secret_id, safe='')}/versions/latest_enabled/access"
        )
    except ApiError as error:
        if error.status == 404:
            return None
        raise
    try:
        return base64.b64decode(doc.get("data") or "", validate=True).decode("utf-8") or None
    except binascii.Error, ValueError, TypeError:
        return None


def _add_version(secrets: Any, secret_id: str, value: str) -> None:
    secrets.request(
        "POST",
        f"/secrets/{quote(secret_id, safe='')}/versions",
        json={"data": base64.b64encode(value.encode("utf-8")).decode("ascii")},
    )


def _create_secret(secrets: Any, project_id: str, path: str, name: str) -> str:
    created = secrets.request(
        "POST",
        "/secrets",
        json={
            "project_id": project_id,
            "name": name,
            "path": path,
            "protected": False,
            "description": "A VM's Scaleway key, written by vecten bootstrap; deploy places it.",
            "tags": ["vecten"],
        },
    )
    return str(created["id"])


_s3_failure = clients.s3_failure
_s3_errors = clients.s3_errors
_s3_status = clients.s3_status


def ensure_state_bucket(s3: Any, name: str, region: str, policy: dict) -> bool:
    """Create the state bucket when absent, then set its policy, versioning and lifecycle.

    The policy goes first: it names the operator, so the settings after it stay reachable.
    Returns whether the bucket was created.
    """
    created = False
    policy_set = False
    try:
        s3.head_bucket(Bucket=name)
        exists = True
    except _s3_errors() as exc:
        code, status = _s3_status(exc)
        if status == 403 or code in ("403", "AccessDenied", "Forbidden"):
            _reclaim(s3, name, policy)
            policy_set = True
            exists = True
        elif status != 404 and code not in ("404", "NoSuchBucket", "NotFound"):
            raise _s3_failure(f"HeadBucket on {name}", exc) from None
        else:
            exists = False
    if not exists:
        try:
            s3.create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": region})
            created = True
        except _s3_errors() as exc:
            if _s3_status(exc)[0] != "BucketAlreadyOwnedByYou":
                raise _s3_failure(f"CreateBucket {name}", exc) from None
    steps: list[tuple[str, Callable[..., Any], dict[str, Any]]] = []
    if not policy_set:
        steps.append(("PutBucketPolicy", s3.put_bucket_policy, {"Policy": json.dumps(policy)}))
    steps += [
        (
            "PutBucketVersioning",
            s3.put_bucket_versioning,
            {"VersioningConfiguration": {"Status": "Enabled"}},
        ),
        (
            "PutBucketLifecycleConfiguration",
            s3.put_bucket_lifecycle_configuration,
            {
                "LifecycleConfiguration": {
                    "Rules": [
                        {
                            "ID": "expire-noncurrent-state",
                            "Status": "Enabled",
                            "Filter": {"Prefix": ""},
                            "NoncurrentVersionExpiration": {
                                "NoncurrentDays": STATE_NONCURRENT_DAYS
                            },
                        }
                    ]
                }
            },
        ),
    ]
    for operation, method, kwargs in steps:
        try:
            method(Bucket=name, **kwargs)
        except _s3_errors() as exc:
            raise _s3_failure(f"{operation} on {name}", exc) from None
    return created


def _reclaim(s3: Any, name: str, policy: dict) -> None:
    """Rewrite the policy of a state bucket that refuses this login.

    The policy names whoever ran bootstrap last, and Scaleway lets the Organization Owner
    rewrite any bucket policy, so the Owner's rerun names the current operator again.
    """
    try:
        s3.put_bucket_policy(Bucket=name, Policy=json.dumps(policy))
    except _s3_errors() as exc:
        code = _s3_status(exc)[0] or type(exc).__name__
        raise Failure(
            ExitCode.PREREQUISITE,
            f"the state bucket {name} refuses your login and its policy could not be rewritten "
            f"({code}): its OperatorFullAccess statement names whoever ran bootstrap last. "
            "Rerun bootstrap as that person, or as the Organization Owner, who can always "
            "rewrite a bucket policy; the rerun names the person running it",
        ) from None


def ack_key(component: str, access: str) -> str:
    """The object a host's watcher writes once it holds the key `access`."""
    digest = hashlib.sha256(access.encode("utf-8")).hexdigest()
    return f"{component}/key-ack/{digest}.json"


def acknowledged(
    region: str, bucket: str, component: str, key: tuple[str, str], project_id: str
) -> bool:
    """Whether the host holding `key` has acknowledged it, asked with that key itself.

    The artifacts bucket's policy admits the host's own key to its component prefix, and
    nobody else's but the deploy key's, which bootstrap does not hold. A missing object, a
    missing bucket and a refusal all mean not yet.
    """
    s3 = clients.object_storage(region, key[0], key[1], project_id)
    try:
        s3.head_object(Bucket=bucket, Key=ack_key(component, key[0]))
    except _s3_errors() as exc:
        code, status = _s3_status(exc)
        if status in (403, 404) or code in ("403", "404", "NoSuchKey", "NoSuchBucket"):
            return False
        raise _s3_failure("HeadObject of a key acknowledgement", exc) from None
    return True


# --- the run -----------------------------------------------------------------------------


@dataclass
class Ledger:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    rotated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


@dataclass
class Prepared:
    """What `prepare` did: the facts to record and the GitHub values to publish."""

    state_bucket: str
    cloud_ids: dict[str, str]
    environment_variables: dict[str, str]
    environment_secrets: dict[str, str] = field(repr=False)
    repository_secrets: dict[str, str] = field(repr=False)
    report: dict[str, Any]


@dataclass(frozen=True)
class Target:
    """One environment as bootstrap sees it."""

    project: str
    env: str
    region: str
    organization: str
    operator: str
    layout: Layout
    dns_zone: str


def _ci_key(
    iam: Any,
    organization: str,
    owner: str,
    app_id: str,
    project_id: str,
    recorded: str | None,
    moment: datetime,
    ledger: Ledger,
    description: str,
) -> tuple[str, tuple[str, str] | None, str, str | None]:
    """The access key GitHub should hold, a new (access, secret) to publish if any, when the
    key GitHub should hold expires, and the newest key it replaced, if any.

    The recorded key is kept while it is fresh and no key of the application is newer: GitHub
    secrets cannot be read back, so a newer key may be what an interrupted run published.
    Older keys are what an earlier run replaced; they are deleted now, one run after GitHub
    stopped handing them out, so a workflow run that started with one has finished with it.
    """
    keys = {str(k.get("access_key")): k for k in list_keys(iam, organization, app_id)}
    current = keys.get(recorded) if recorded else None
    if current is not None and _fresh(current, moment):
        since = _time(current.get("created_at"))
        others = {a: k for a, k in keys.items() if a != recorded}
        newer = [
            a
            for a, k in others.items()
            if since is not None and (_time(k.get("created_at")) or since) > since
        ]
        if not newer:
            for access in others:
                delete_key(iam, access)
                ledger.deleted.append(f"{owner}_retired_key")
            expires = _time(current.get("expires_at"))
            assert expires is not None
            return str(recorded), None, _ts(expires), None
    new = create_key(iam, app_id, project_id, description, moment)
    (ledger.rotated if recorded or keys else ledger.created).append(f"{owner}_key")
    replaced = max(keys.values(), key=lambda k: str(k.get("created_at") or ""), default=None)
    retiring = str(replaced["access_key"]) if replaced is not None else None
    return new[0], new, _ts(moment + KEY_LIFETIME), retiring


def _host_key(
    target: Target,
    iam: Any,
    secrets: Any,
    host: str,
    app_id: str,
    resources_id: str,
    artifacts: str,
    moment: datetime,
    ledger: Ledger,
) -> tuple[str | None, str]:
    """Reconcile host `host`'s key; returns the access key still awaiting retirement, if any,
    and when the current key expires.

    The current key is the one in the newest version of `HOST_KEY_<HOST>`, which is what
    deploy places. A missing or near-expiry one is replaced by a new key stored as a new
    version. Keys older than the current one are retired once the host acknowledges the
    current key; keys newer than it never reached the secret, so no host holds them.
    """
    path = scaleway_secret_path(target.project, target.env, HOST_KEY_SCOPE)
    name = host_key_name(host)
    secret_id = find_secret(secrets, resources_id, path, name)
    stored = read_latest(secrets, secret_id) if secret_id else None
    current: tuple[str, str] | None = None
    if stored and ":" in stored:
        access, secret = stored.split(":", 1)
        current = (access, secret)
    keys = {str(k.get("access_key")): k for k in list_keys(iam, target.organization, app_id)}
    current_key = keys.get(current[0]) if current else None
    made_now = False
    if current_key is None or not _fresh(current_key, moment):
        current = create_key(iam, app_id, resources_id, f"VM key of {host} in {target.env}", moment)
        if secret_id is None:
            secret_id = _create_secret(secrets, resources_id, path, name)
        _add_version(secrets, secret_id, f"{current[0]}:{current[1]}")
        (ledger.rotated if stored else ledger.created).append(f"{host}_key")
        current_created: datetime | None = moment
        made_now = True
        current_expires = _ts(moment + KEY_LIFETIME)
    else:
        current_created = _time(current_key.get("created_at"))
        fresh_until = _time(current_key.get("expires_at"))
        assert fresh_until is not None
        current_expires = _ts(fresh_until)
    assert current is not None
    retiring: list[dict] = []
    for access, key in keys.items():
        if access == current[0]:
            continue
        expires = _time(key.get("expires_at"))
        created = _time(key.get("created_at"))
        newer = created is not None and current_created is not None and created > current_created
        if newer or (expires is not None and expires <= moment):
            delete_key(iam, access)
            ledger.deleted.append(f"{host}_stale_key")
            continue
        retiring.append(key)
    if not retiring:
        return None, current_expires
    component = target.layout.hosts[host][0]
    if not made_now and acknowledged(target.region, artifacts, component, current, resources_id):
        for key in retiring:
            delete_key(iam, str(key["access_key"]))
            ledger.deleted.append(f"{host}_retired_key")
        return None, current_expires
    newest = max(retiring, key=lambda k: str(k.get("created_at") or ""))
    return str(newest["access_key"]), current_expires


def _drop_host(
    iam: Any, secrets: Any, target: Target, resources_id: str, host: str, ledger: Ledger
) -> None:
    """Delete the application, policy, keys and stored key of a host the module does not
    declare."""
    name = f"{name_prefix(target.project, target.env)}-{host}"
    app_id = find_application(iam, target.organization, name)
    if app_id:
        for key in list_keys(iam, target.organization, app_id):
            delete_key(iam, str(key["access_key"]))
            ledger.deleted.append(f"{host}_key")
        policy_id = _policy_of(iam, target.organization, app_id, name)
        if policy_id:
            iam.request("DELETE", f"/policies/{quote(policy_id, safe='')}")
            ledger.deleted.append(f"{host}_policy")
        iam.request("DELETE", f"/applications/{quote(app_id, safe='')}")
        ledger.deleted.append(f"{host}_application")
    path = scaleway_secret_path(target.project, target.env, HOST_KEY_SCOPE)
    secret_id = find_secret(secrets, resources_id, path, host_key_name(host))
    if secret_id:
        secrets.request("DELETE", f"/secrets/{quote(secret_id, safe='')}")
        ledger.deleted.append(host_key_name(host))


def prepare(target: Target, recorded: dict[str, str]) -> Prepared:
    """Create or reconcile everything `bootstrap` owns for `target`.

    `recorded` is the environment's `cloud_ids` from vecten.toml. Nothing here writes to
    GitHub or vecten.toml; the caller publishes `Prepared`.
    """
    moment = now()
    ledger = Ledger()
    project, env = target.project, target.env
    prefix = name_prefix(project, env)
    account = clients.client("account")
    iam = clients.client("iam")
    secrets = clients.client("secret-manager", target.region)

    # The zone and both projects are checked before anything is created, so a refusal
    # creates nothing.
    dns_id = find_dns_project(clients.client("domain"), target.organization, target.dns_zone)
    wanted = {
        role: (scaleway_project_name(project, env, role), project_marker(role, project, env))
        for role in ("resources", "state")
    }
    found = {
        role: find_project(account, target.organization, name, marker)
        for role, (name, marker) in wanted.items()
    }
    ids: dict[str, str] = {}
    for role, (name, marker) in wanted.items():
        existing = found[role]
        if existing is None:
            existing = create_project(account, target.organization, name, marker)
            ledger.created.append("project" if role == "resources" else "state_project")
        ids[role] = existing
    resources_id, state_id = ids["resources"], ids["state"]

    owners: dict[str, list[dict]] = {
        "deploy": deploy_rules(resources_id, state_id, dns_id),
        "plan": plan_rules(state_id),
    }
    for host in target.layout.hosts:
        owners[host] = host_rules(target.layout, project, env, resources_id, host)
    descriptions = {
        "deploy": "applies Terraform, places VM keys and ships from GitHub Actions",
        "plan": "plans pull requests from GitHub Actions",
    }
    apps: dict[str, str] = {}
    for owner, rules in owners.items():
        name = f"{prefix}-{owner}"
        about = descriptions.get(owner, f"the {owner} VM")
        app_id, made = ensure_application(
            iam, target.organization, name, f"vecten {project} {env}: {about}"
        )
        apps[owner] = app_id
        if made:
            ledger.created.append(f"{owner}_application")
        outcome = ensure_policy(iam, target.organization, app_id, name, rules)
        if outcome == "created":
            ledger.created.append(f"{owner}_policy")
        elif outcome == "updated":
            ledger.updated.append(f"{owner}_policy")

    bucket = scaleway_bucket_name(project, env, "state", state_id)
    creds = clients.credentials()
    state_s3 = clients.object_storage(target.region, creds.access_key, creds.secret_key, state_id)
    policy = state_bucket_policy(
        bucket, env, deploy_app=apps["deploy"], plan_app=apps["plan"], operator=target.operator
    )
    if ensure_state_bucket(state_s3, bucket, target.region, policy):
        ledger.created.append("state_bucket")

    environment_secrets: dict[str, str] = {}
    repository_secrets: dict[str, str] = {}
    retiring: dict[str, str] = {}
    expiries: dict[str, str] = {}
    deploy_access, new, expiries["deploy"], replaced = _ci_key(
        iam,
        target.organization,
        "deploy",
        apps["deploy"],
        resources_id,
        recorded.get("key_deploy"),
        moment,
        ledger,
        f"vecten deploy key of {env}",
    )
    if new:
        environment_secrets = {ENV_ACCESS_SECRET: new[0], ENV_SECRET_SECRET: new[1]}
    if replaced:
        retiring["deploy"] = replaced
    plan_access, new, expiries["plan"], replaced = _ci_key(
        iam,
        target.organization,
        "plan",
        apps["plan"],
        resources_id,
        recorded.get("key_plan"),
        moment,
        ledger,
        f"vecten plan key of {env}",
    )
    if new:
        access_name, secret_name = plan_secret_names(env)
        repository_secrets = {access_name: new[0], secret_name: new[1]}
    if replaced:
        retiring["plan"] = replaced

    artifacts = scaleway_bucket_name(project, env, "artifacts", resources_id)
    awaiting_ack: list[str] = []
    for host in target.layout.hosts:
        waiting, expiries[host] = _host_key(
            target, iam, secrets, host, apps[host], resources_id, artifacts, moment, ledger
        )
        if waiting:
            retiring[host] = waiting
            awaiting_ack.append(host)
    for host in HOST_KEYS:
        if host not in target.layout.hosts:
            _drop_host(iam, secrets, target, resources_id, host, ledger)

    cloud_ids = {
        "project": resources_id,
        "state_project": state_id,
        "dns_project": dns_id,
        **operator_ids(target.operator),
        **{f"app_{owner}": app_id for owner, app_id in apps.items()},
        "key_deploy": deploy_access,
        "key_plan": plan_access,
        **{f"retiring_{owner}": access for owner, access in retiring.items()},
        **{f"{CLOUD_ID_EXPIRY_PREFIX}{owner}": when for owner, when in expiries.items()},
    }
    report = {
        "project_id": resources_id,
        "state_project_id": state_id,
        "dns_project_id": dns_id,
        "deploy_application": apps["deploy"],
        "plan_application": apps["plan"],
        "host_applications": {h: apps[h] for h in target.layout.hosts},
        "created": ledger.created,
        "updated": ledger.updated,
        "rotated": ledger.rotated,
        "deleted": ledger.deleted,
        "awaiting_ack": awaiting_ack,
        "ci_keys_retiring": [o for o in ("deploy", "plan") if o in retiring],
        "environment_secrets": list(environment_secrets),
        "repository_secrets": list(repository_secrets),
    }
    return Prepared(
        state_bucket=bucket,
        cloud_ids=cloud_ids,
        environment_variables={
            PROJECT_VARIABLE: resources_id,
            STATE_PROJECT_VARIABLE: state_id,
            REGION_VARIABLE: target.region,
            STATE_BUCKET_VARIABLE: bucket,
        },
        environment_secrets=environment_secrets,
        repository_secrets=repository_secrets,
        report=report,
    )
