"""Cloud resource names derived from the project and environment names."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from vecten_shared.output import ExitCode, Failure

BUCKET_NAME_MAX = 63
PREFIX = "vecten-"
# 44 characters of project and env slugs, the hyphen between them and `vecten-` make at most
# 52, so `<prefix>-warehouse` stays within RDS's 63-character identifier limit.
SLUG_BUDGET = 44
_NOT_SLUG = re.compile(r"[^a-z0-9]+")
_ACCOUNT_ID = re.compile(r"\d{12}")
_SCW_PROJECT_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
SCALEWAY_PROJECT_KINDS = ("resources", "state")
SCALEWAY_BUCKET_KINDS = ("state", "artifacts")


def slug(value: str) -> str:
    """Lowercase, each run of characters outside `[a-z0-9]` as one `-`, no edge hyphens."""
    return _NOT_SLUG.sub("-", value.lower()).strip("-")


def _project_part(project: str, env_slug: str, room: int) -> str:
    part = slug(project)[: min(room, SLUG_BUDGET - len(env_slug))].rstrip("-")
    if not part or not env_slug:
        raise Failure(
            ExitCode.CONFIG,
            f"project {project!r} and environment slugs must each keep a letter or digit",
        )
    return part


def name_prefix(project: str, env: str) -> str:
    """`vecten-<project slug>-<env slug>`, at most 52 characters.

    RDS and snapshot identifiers start with a letter, hold only `[a-z0-9-]`, contain no `--`,
    do not end in `-` and are at most 63 characters; every name built here keeps that. The
    project slug is cut to `44 - len(env slug)` characters, then a trailing `-` is dropped.
    The AWS Terraform module computes the same prefix, and `destroy` finds the RDS instances
    by `rds_identifier`, so the two must stay identical:

        env_slug     = trim(replace(lower(var.environment), "/[^a-z0-9]+/", "-"), "-")
        project_slug = trim(replace(lower(var.project_name), "/[^a-z0-9]+/", "-"), "-")
        project_part = trim(substr(local.project_slug, 0, 44 - length(local.env_slug)), "-")
        name_prefix  = "vecten-${local.project_part}-${local.env_slug}"

    `substr(s, 0, n)` equals `s[:n]` here because `n` is at least 12 and `s` holds no
    multi-byte characters; `trim(..., "-")` equals `rstrip("-")` because a slug never starts
    with `-`.
    """
    env_slug = slug(env)
    return f"{PREFIX}{_project_part(project, env_slug, SLUG_BUDGET)}-{env_slug}"


def rds_identifier(project: str, env: str, key: str) -> str:
    """The RDS instance identifier of instance `key` (`state`, `warehouse`); mirrored in HCL."""
    return f"{name_prefix(project, env)}-{key}"


def state_bucket_name(project: str, env: str, account_id: str) -> str:
    """The Terraform state bucket, at most 63 characters, the account id kept whole.

    S3 bucket names are global, so the account id is what keeps two accounts deploying the same
    project name apart; the project slug shrinks further when the prefix leaves no room for it.
    """
    return _bucket_name(project, env, account_id, "state")


def artifacts_bucket_name(project: str, env: str, account_id: str) -> str:
    """The artifacts and logs bucket the AWS module creates; mirrors `local.artifacts_bucket`."""
    return _bucket_name(project, env, account_id, "artifacts")


def _bucket_name(project: str, env: str, account_id: str, kind: str) -> str:
    if not _ACCOUNT_ID.fullmatch(account_id):
        raise Failure(ExitCode.INTERNAL, "an AWS account id is exactly 12 digits")
    return _suffixed_bucket_name(project, env, kind, account_id)


def _suffixed_bucket_name(project: str, env: str, kind: str, suffix: str) -> str:
    env_slug = slug(env)
    tail = f"-{env_slug}-{kind}-{suffix}"
    room = BUCKET_NAME_MAX - len(PREFIX) - len(tail)
    return f"{PREFIX}{_project_part(project, env_slug, room)}{tail}"


def scaleway_project_name(project: str, env: str, kind: str) -> str:
    """`<prefix>` for the resources project, `<prefix>-state` for the state project.

    At most 58 characters, under the 64 a Scaleway project name allows.
    """
    if kind not in SCALEWAY_PROJECT_KINDS:
        raise Failure(
            ExitCode.INTERNAL, f"a Scaleway project kind is one of {SCALEWAY_PROJECT_KINDS}"
        )
    prefix = name_prefix(project, env)
    return prefix if kind == "resources" else f"{prefix}-state"


def scaleway_bucket_name(project: str, env: str, kind: str, project_id: str) -> str:
    """`vecten-<project>-<env>-<kind>-<8 hex>`, at most 63 characters.

    Bucket names are global across Scaleway, so the first 8 hex characters of the ID of the
    project holding the bucket keep two organisations deploying the same project name apart.
    The Scaleway module computes the same name from `substr(lower(project_id), 0, 8)`.
    """
    if kind not in SCALEWAY_BUCKET_KINDS:
        raise Failure(
            ExitCode.INTERNAL, f"a Scaleway bucket kind is one of {SCALEWAY_BUCKET_KINDS}"
        )
    if not _SCW_PROJECT_ID.fullmatch(project_id):
        raise Failure(ExitCode.INTERNAL, "a Scaleway project id is a UUID")
    return _suffixed_bucket_name(project, env, kind, project_id[:8].lower())


def deploy_role_name(project: str, env: str) -> str:
    """The IAM role CI assumes over OIDC; at most 59 characters, under IAM's 64."""
    return f"{name_prefix(project, env)}-deploy"


def plan_role_name(project: str, env: str) -> str:
    """The read-only IAM role the plan workflow assumes; at most 57 characters."""
    return f"{name_prefix(project, env)}-plan"


def boundary_policy_name(project: str, env: str) -> str:
    """The permissions boundary every host role carries; at most 61 characters, under 128."""
    return f"{name_prefix(project, env)}-boundary"


def snapshot_name(project: str, env: str, key: str, now: datetime) -> str:
    """The final snapshot `destroy` takes of instance `key`, stamped with `now` in UTC.

    At most 83 characters, so within the 255 a snapshot identifier allows, with the same
    character rules as the instance identifier.
    """
    if now.tzinfo is None:
        raise ValueError("snapshot_name needs a timezone-aware datetime")
    stamp = now.astimezone(UTC).strftime("%Y%m%d%H%M%S")
    return f"{rds_identifier(project, env, key)}-final-{stamp}"
