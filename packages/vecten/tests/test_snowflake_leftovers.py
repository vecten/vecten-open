"""What stays in a shared Snowflake account outside Terraform, read by ownership."""

import pytest

from vecten.deploy.snowflake_leftovers import Leftovers, read_leftovers
from vecten_shared.config import VectenConfig
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import snowflake_names

KEY = (
    "-----BEGIN PRIVATE KEY-----\nMIIBVQIBADANBgkqhkiG9w0BAQEFAASCAT8wggE7AgEAAkEAu1SU1LfVLPHCozMx"
    "\n-----END PRIVATE KEY-----\n"
)


def names():
    config = VectenConfig.model_validate(
        {
            "users": 1,
            "project": {"name": "acme", "scaffold_version": "0.1.1"},
            "warehouse": {"kind": "snowflake", "account": "ACME-SHARED", "shared_account": True},
        }
    )
    return snowflake_names(config, "dev")


class Snowflake:
    """Answers the deploy user's SHOW statements from `roles`, `grants_to` and `grants_of`."""

    def __init__(self, roles=("PROVISIONER_ACME_DEV",), grants_to=(), grants_of=()):
        self.roles, self.grants_to, self.grants_of = roles, grants_to, grants_of
        self.queries, self.logins = [], []

    def connect(self, **kwargs):
        self.logins.append(kwargs)
        return self

    def cursor(self):
        return self

    def execute(self, query):
        self.queries.append(query)
        self.query = query
        return self

    @property
    def description(self):
        if self.query.startswith("SHOW ROLES"):
            return [("name",), ("owner",)]
        if self.query.startswith("SHOW GRANTS OF"):
            return [("role",), ("granted_to",), ("grantee_name",)]
        return [("privilege",), ("granted_on",), ("name",)]

    def fetchall(self):
        if self.query.startswith("SHOW ROLES"):
            return [(role, "DEPLOY") for role in self.roles]
        if self.query.startswith("SHOW GRANTS OF"):
            return [("PROVISIONER_ACME_DEV", kind, name) for kind, name in self.grants_of]
        return list(self.grants_to)

    def close(self):
        pass


CREDENTIALS = {
    "SNOWFLAKE_ORGANIZATION_NAME": "ACME",
    "SNOWFLAKE_ACCOUNT_NAME": "SHARED",
    "SNOWFLAKE_USER": "DEPLOY_DEV",
    "SNOWFLAKE_PRIVATE_KEY": KEY,
}


def test_the_listing_is_what_the_provisioner_owns_and_nothing_named_alike():
    snowflake = Snowflake(
        grants_to=[
            ("OWNERSHIP", "USER", "JANE_ACME_DEV"),
            ("OWNERSHIP", "ROLE", "FINANCE_ACME_DEV"),
            ("USAGE", "DATABASE", "ACME_DEV"),
            ("USAGE", "ROLE", "ANALYST_ACME_DEV"),
        ],
        grants_of=[("USER", "PROVISIONER_ACME_DEV"), ("ROLE", "SYSADMIN")],
    )
    found = read_leftovers(CREDENTIALS, names(), connect=snowflake.connect, key=lambda pem: "K")
    assert found == Leftovers(
        names=names(),
        owned=(("USER", "JANE_ACME_DEV"), ("ROLE", "FINANCE_ACME_DEV")),
        provisioner_user=True,
    )
    assert snowflake.queries == [
        "SHOW ROLES LIKE 'PROVISIONER_ACME_DEV'",
        'SHOW GRANTS TO ROLE "PROVISIONER_ACME_DEV"',
        'SHOW GRANTS OF ROLE "PROVISIONER_ACME_DEV"',
    ]
    # Ownership alone finds them: `_X_ACME_DEV` ends in `_ACME_DEV` too.
    assert not any("%" in query for query in snowflake.queries)
    (login,) = snowflake.logins
    assert (login["account"], login["user"], login["private_key"]) == (
        "ACME-SHARED",
        "DEPLOY_DEV",
        "K",
    )


def test_an_environment_without_its_provisioner_role_leaves_nothing_it_owns():
    snowflake = Snowflake(roles=())
    found = read_leftovers(CREDENTIALS, names(), connect=snowflake.connect, key=lambda pem: "K")
    assert found.owned == () and found.provisioner_user is False
    assert len(snowflake.queries) == 1


def test_the_statements_drop_every_owned_object_then_the_provisioner_and_its_integration():
    found = Leftovers(
        names=names(),
        owned=(("USER", "JANE_ACME_DEV"), ("ROLE", "FINANCE_ACME_DEV")),
        provisioner_user=True,
    )
    assert found.statements() == [
        'DROP USER "JANE_ACME_DEV";',
        'DROP ROLE "FINANCE_ACME_DEV";',
        # Its key-pair-only policy lives in the database `--delete-data` drops.
        "ALTER USER IF EXISTS PROVISIONER_ACME_DEV UNSET AUTHENTICATION POLICY;",
        "DROP USER IF EXISTS PROVISIONER_ACME_DEV;",
        "DROP SECURITY INTEGRATION IF EXISTS EXTERNAL_OAUTH_ACME_DEV;",
    ]
    assert found.blocks_data_deletion
    assert not Leftovers(names=names(), owned=(), provisioner_user=False).blocks_data_deletion
    assert Leftovers(names=names(), owned=(), provisioner_user=True).blocks_data_deletion


def test_a_failed_read_is_a_tool_failure_naming_the_role():
    def refuse(**kwargs):
        raise RuntimeError("network down")

    with pytest.raises(Failure) as caught:
        read_leftovers(CREDENTIALS, names(), connect=refuse, key=lambda pem: "K")
    assert caught.value.code == ExitCode.TOOL_FAILED
    assert "PROVISIONER_ACME_DEV" in caught.value.message
