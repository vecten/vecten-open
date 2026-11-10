import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from vecten.deploy.names import (
    artifacts_bucket_name,
    boundary_policy_name,
    deploy_role_name,
    name_prefix,
    plan_role_name,
    rds_identifier,
    scaleway_bucket_name,
    scaleway_project_name,
    slug,
    snapshot_name,
    state_bucket_name,
)
from vecten_shared.output import ExitCode, Failure

ACCOUNT = "123456789012"
NOW = datetime(2026, 10, 17, 9, 5, 3, tzinfo=UTC)


def test_name_prefix_lowercases_and_hyphenates():
    assert name_prefix("acme_data", "prod") == "vecten-acme-data-prod"
    assert name_prefix("Acme", "staging") == "vecten-acme-staging"


def test_rds_identifier_appends_the_instance_key():
    assert rds_identifier("acme_data", "prod", "warehouse") == "vecten-acme-data-prod-warehouse"


def test_state_bucket_name():
    assert state_bucket_name("acme_data", "prod", ACCOUNT) == (
        f"vecten-acme-data-prod-state-{ACCOUNT}"
    )


def test_long_project_truncated_with_account_intact():
    name = state_bucket_name("a" * 40 + "_" + "b" * 22, "production", ACCOUNT)
    assert len(name) <= 63
    assert name.endswith(f"-production-state-{ACCOUNT}")
    assert name.startswith("vecten-aaaa")
    assert "_" not in name and name == name.lower()


def test_truncation_never_leaves_a_hyphen_before_the_env():
    env = "e" * 10
    # 63 - len("vecten-") - len(f"-{env}-state-{ACCOUNT}") = 26 characters of project
    # survive, so the cut lands right after the underscore that becomes a hyphen.
    project = "a" * 25 + "_" + "b" * 30
    name = state_bucket_name(project, env, ACCOUNT)
    assert len(name) <= 63
    assert name == f"vecten-{'a' * 25}-{env}-state-{ACCOUNT}"


def test_longest_env_still_fits():
    name = state_bucket_name("p" * 63, "e" * 32, ACCOUNT)
    assert len(name) <= 63 and name.startswith("vecten-p")


def test_deploy_role_name():
    assert deploy_role_name("acme_data", "prod") == "vecten-acme-data-prod-deploy"
    long = deploy_role_name("p" * 63, "e" * 32)
    assert len(long) <= 64 and long.endswith("-" + "e" * 32 + "-deploy")
    assert long.startswith("vecten-p")


def test_artifacts_bucket_name_mirrors_the_module():
    assert artifacts_bucket_name("acme", "prod", ACCOUNT) == f"vecten-acme-prod-artifacts-{ACCOUNT}"
    # HCL: room = 63 - 7 - len("-production-artifacts-<account>") = 22 characters of project.
    name = artifacts_bucket_name("a" * 40, "production", ACCOUNT)
    assert name == f"vecten-{'a' * 22}-production-artifacts-{ACCOUNT}"
    assert len(name) == 63


def test_plan_role_and_boundary_names():
    assert plan_role_name("acme_data", "prod") == "vecten-acme-data-prod-plan"
    assert boundary_policy_name("acme_data", "prod") == "vecten-acme-data-prod-boundary"
    assert len(plan_role_name("p" * 63, "e" * 32)) <= 64


def test_snapshot_name():
    name = snapshot_name("acme_data", "prod", "warehouse", NOW)
    assert name == "vecten-acme-data-prod-warehouse-final-20261017090503"
    assert re.fullmatch(r"vecten-[a-z0-9-]+-final-\d{14}", name)
    assert name.startswith(rds_identifier("acme_data", "prod", "warehouse") + "-final-")
    assert len(snapshot_name("p" * 63, "e" * 32, "warehouse", NOW)) <= 255


def test_snapshot_name_uses_utc():
    plus_two = NOW.astimezone(timezone(timedelta(hours=2)))
    assert snapshot_name("acme", "prod", "state", plus_two).endswith("-final-20261017090503")


def test_snapshot_name_refuses_a_naive_time():
    with pytest.raises(ValueError):
        snapshot_name("acme", "prod", "state", datetime(2026, 10, 17))


RDS_IDENTIFIER = re.compile(r"^[a-z](?!.*--)[a-z0-9-]{0,62}(?<!-)$")


def test_slug():
    assert slug("Acme__Data--x_") == "acme-data-x"
    assert slug("-a-") == "a"


def test_trailing_underscore_leaves_no_hyphen():
    assert name_prefix("acme_", "prod") == "vecten-acme-prod"


def test_mixed_separators_collapse():
    assert name_prefix("a-_b", "prod") == "vecten-a-b-prod"


def test_env_trailing_hyphen_dropped():
    prefix = name_prefix("acme", "prod-")
    assert prefix == "vecten-acme-prod"
    assert "--" not in rds_identifier("acme", "prod-", "state")


def test_longest_names_fit_the_prefix_budget():
    prefix = name_prefix("p" * 63, "e" * 32)
    assert len(prefix) <= 52
    assert prefix == "vecten-" + "p" * 12 + "-" + "e" * 32
    assert RDS_IDENTIFIER.fullmatch(rds_identifier("p" * 63, "e" * 32, "warehouse"))


@pytest.mark.parametrize(
    ("project", "env"),
    [
        ("acme", "prod"),
        ("acme_", "prod"),
        ("a-_b", "prod-"),
        ("a__b--c__", "staging"),
        ("a" * 43 + "_" + "b" * 19, "e" * 32),
        ("x" + "_-" * 31, "p-1"),
        ("p" * 63, "e" * 32),
        ("p" * 11 + "_" + "q" * 51, "e" * 32),
    ],
)
@pytest.mark.parametrize("key", ["state", "warehouse"])
def test_rds_identifiers_are_legal(project, env, key):
    identifier = rds_identifier(project, env, key)
    assert RDS_IDENTIFIER.fullmatch(identifier), identifier
    snapshot = snapshot_name(project, env, key, NOW)
    assert len(snapshot) <= 255 and "--" not in snapshot and not snapshot.endswith("-")
    bucket = state_bucket_name(project, env, ACCOUNT)
    assert len(bucket) <= 63 and "--" not in bucket and bucket.endswith(ACCOUNT)
    assert len(deploy_role_name(project, env)) <= 64


@pytest.mark.parametrize("account", ["12345678901", "1234567890123", "12345678901a", ""])
def test_account_id_must_be_twelve_digits(account):
    with pytest.raises(Failure) as exc:
        state_bucket_name("acme", "prod", account)
    assert exc.value.code == ExitCode.INTERNAL


SCW_PROJECT = "1a2b3c4d-5e6f-4711-8899-aabbccddeeff"
SCW_BUCKET = re.compile(r"^[a-z0-9](?!.*--)[a-z0-9-]{1,61}[a-z0-9]$")


def test_scaleway_project_names():
    assert scaleway_project_name("acme_data", "prod", "resources") == "vecten-acme-data-prod"
    assert scaleway_project_name("acme_data", "prod", "state") == "vecten-acme-data-prod-state"
    longest = scaleway_project_name("p" * 63, "e" * 32, "state")
    assert len(longest) <= 64 and longest.endswith("-" + "e" * 32 + "-state")


def test_scaleway_project_name_refuses_an_unknown_kind():
    with pytest.raises(Failure) as exc:
        scaleway_project_name("acme", "prod", "logs")
    assert exc.value.code == ExitCode.INTERNAL


def test_scaleway_bucket_name_carries_eight_hex_of_the_project_id():
    assert scaleway_bucket_name("acme_data", "prod", "state", SCW_PROJECT) == (
        "vecten-acme-data-prod-state-1a2b3c4d"
    )
    assert scaleway_bucket_name("acme", "prod", "artifacts", SCW_PROJECT.upper()) == (
        "vecten-acme-prod-artifacts-1a2b3c4d"
    )


@pytest.mark.parametrize(
    ("project", "env"),
    [
        ("acme", "prod"),
        ("a-_b", "prod-"),
        ("p" * 63, "e" * 32),
        ("a" * 43 + "_" + "b" * 19, "e" * 32),
    ],
)
@pytest.mark.parametrize("kind", ["state", "artifacts"])
def test_scaleway_bucket_names_are_legal(project, env, kind):
    name = scaleway_bucket_name(project, env, kind, SCW_PROJECT)
    assert 3 <= len(name) <= 63 and SCW_BUCKET.fullmatch(name), name
    assert name.endswith(f"-{kind}-1a2b3c4d") and name.startswith("vecten-")


@pytest.mark.parametrize(
    "project_id", ["", "1a2b3c4d", "not-a-uuid-at-all-0000-000000000000", "1a2b3c4d5e6f4711"]
)
def test_scaleway_bucket_name_needs_a_project_uuid(project_id):
    with pytest.raises(Failure) as exc:
        scaleway_bucket_name("acme", "prod", "state", project_id)
    assert exc.value.code == ExitCode.INTERNAL


SHAPE_TEST = Path(__file__).resolve().parents[3] / "terraform/scaleway/tests/shape.tftest.hcl"


def _long_name_run() -> dict[str, str]:
    """The Scaleway module test's long-name inputs and the names it asserts."""
    text = SHAPE_TEST.read_text(encoding="utf-8")
    block = text[text.index('run "long_project_name_keeps_names_legal"') :]
    block = block[: block.index("\nrun ") if "\nrun " in block else len(block)]
    found = {
        "project_id": re.search(r'project_id\s*=\s*"([^"]+)"', text).group(1),
        "project_name": re.search(r'project_name\s*=\s*"([^"]+)"', block).group(1),
        "environment": re.search(r'environment\s*=\s*"([^"]+)"', block).group(1),
        "bucket": re.search(r'output\.bucket\s*==\s*"([^"]+)"', block).group(1),
        "warehouse": re.search(r'\.name\s*==\s*"([^"]+)"', block).group(1),
    }
    return found


def test_the_scaleway_names_match_the_module_test_for_a_long_project_name():
    run = _long_name_run()
    name = scaleway_bucket_name(
        run["project_name"], run["environment"], "artifacts", run["project_id"]
    )
    assert name == run["bucket"]
    assert len(name) <= 63
    assert rds_identifier(run["project_name"], run["environment"], "warehouse") == run["warehouse"]
